import numpy as np
from copy import deepcopy
from itertools import product
from sklearn.preprocessing import OneHotEncoder
from nonconformist.icp import IcpRegressor, IcpClassifier
from nonconformist.nc import RegressorNc, ClassifierNc, MarginErrFunc

from lightwood.mixers.nn import NnMixer
from mindsdb_native.libs.constants.mindsdb import *
from mindsdb_native.libs.phases.base_module import BaseModule
from mindsdb_native.libs.helpers.accuracy_stats import AccStats
from mindsdb_native.libs.helpers.general_helpers import pickle_obj, evaluate_accuracy
from mindsdb_native.libs.helpers.conformal_helpers import clean_df, get_conf_range
from mindsdb_native.libs.helpers.conformal_helpers import BoostedAbsErrorErrFunc, SelfawareNormalizer
from mindsdb_native.libs.helpers.conformal_helpers import ConformalClassifierAdapter, ConformalRegressorAdapter


class ModelAnalyzer(BaseModule):
    def run(self):
        np.seterr(divide='warn', invalid='warn')
        np.random.seed(0)
        """
        # Runs the model on the validation set in order to evaluate the accuracy and confidence of future predictions
        """
        validation_df = self.transaction.input_data.validation_df
        if self.transaction.lmd['tss']['is_timeseries']:
            validation_df = self.transaction.input_data.validation_df[self.transaction.input_data.validation_df['make_predictions'] == True]

        test_df = self.transaction.input_data.test_df
        if self.transaction.lmd['tss']['is_timeseries']:
            test_df = self.transaction.input_data.test_df[self.transaction.input_data.test_df['make_predictions'] == True]

        output_columns = self.transaction.lmd['predict_columns']
        input_columns = [col for col in self.transaction.lmd['columns'] if col not in output_columns and col not in self.transaction.lmd['columns_to_ignore']]

        # Make predictions on the validation and test datasets
        self.transaction.model_backend.predictor.config['include_extra_data'] = True
        normal_predictions = self.transaction.model_backend.predict('validate')
        normal_predictions_test = self.transaction.model_backend.predict('test')
        self.transaction.lmd['test_data_plot'] = {}

        # conformal prediction confidence estimation
        self.transaction.hmd['label_encoders'] = {}
        self.transaction.hmd['icp'] = {'active': False}

        for target in output_columns:
            typing_info = self.transaction.lmd['stats_v2'][target]['typing']
            data_type = typing_info['data_type']
            data_subtype = typing_info['data_subtype']

            is_classification = (data_type == DATA_TYPES.CATEGORICAL) or \
                                (data_type == DATA_TYPES.SEQUENTIAL and
                                 DATA_TYPES.CATEGORICAL in typing_info['data_type_dist'].keys())

            fit_params = {
                'columns_to_ignore': [],
                'nr_preds': self.transaction.lmd['tss'].get('nr_predictions', 0)
            }
            fit_params['columns_to_ignore'].extend([col for col in output_columns if col != target])
            fit_params['columns_to_ignore'].extend([f'{target}_timestep_{i}' for i in range(1, fit_params['nr_preds'])])

            if is_classification:
                if data_subtype != DATA_SUBTYPES.TAGS:
                    all_classes = np.array(self.transaction.lmd['stats_v2'][target]['histogram']['x'])
                    enc = OneHotEncoder(sparse=False, handle_unknown='ignore')
                    enc.fit(all_classes.reshape(-1, 1))
                    self.transaction.hmd['label_encoders'][target] = enc
                else:
                    self.transaction.hmd['label_encoders'][target] = None

                adapter = ConformalClassifierAdapter
                nc_function = MarginErrFunc()
                nc_class = ClassifierNc
                icp_class = IcpClassifier

            else:
                adapter = ConformalRegressorAdapter
                nc_function = BoostedAbsErrorErrFunc()
                nc_class = RegressorNc
                icp_class = IcpRegressor

            if data_type in (DATA_TYPES.NUMERIC, DATA_TYPES.SEQUENTIAL) or (is_classification and data_subtype != DATA_SUBTYPES.TAGS):
                model = adapter(self.transaction.model_backend.predictor)

                if isinstance(self.transaction.model_backend.predictor._mixer, NnMixer) and \
                        self.transaction.model_backend.predictor._mixer.is_selfaware:
                    norm_params = {'output_column': target}
                    normalizer = SelfawareNormalizer(fit_params=norm_params)
                else:
                    normalizer = None

                if normalizer is not None:
                    normalizer.prediction_cache = normal_predictions

                if not is_classification:
                    self.transaction.lmd['stats_v2'][target]['train_std_dev'] = self.transaction.input_data.train_df[target].std()

                nc = nc_class(model, nc_function, normalizer=normalizer)
                icp = icp_class(nc)

                if is_classification:
                    icp.nc_function.model.prediction_cache = np.array(normal_predictions[f'{target}_class_distribution'])
                    icp.nc_function.model.class_map = self.transaction.lmd['stats_v2'][target]['lightwood_class_map']
                else:
                    if self.transaction.lmd['tss']['is_timeseries'] and self.transaction.lmd['tss']['nr_predictions'] > 1:
                        # time series confidence bounds only at t+1 forecast
                        icp.nc_function.model.prediction_cache = np.array([p[0] for p in normal_predictions[target]])
                    else:
                        icp.nc_function.model.prediction_cache = np.array(normal_predictions[target])

                # check if we need more than one ICP depending on grouped by columns for time series tasks
                if self.transaction.lmd['tss']['is_timeseries'] and self.transaction.lmd['tss']['group_by']:
                    self.transaction.hmd['icp'][target] = {}
                    group_info = self.transaction.input_data.train_df[self.transaction.lmd['tss']['group_by']].to_dict('list')
                    all_group_combinations = list(product(*[set(x) for x in group_info.values()]))
                    group_keys = [x for x in group_info.keys()]
                    self.transaction.hmd['icp'][target]['__groups'] = all_group_combinations
                    self.transaction.hmd['icp'][target]['__group_keys'] = group_keys
                    for combination in all_group_combinations:
                        self.transaction.hmd['icp'][target][frozenset(combination)] = deepcopy(icp)
                        self.transaction.hmd['icp'][target][frozenset(combination)].fit(None, None)
                else:
                    self.transaction.hmd['icp'][target] = icp
                    self.transaction.hmd['icp'][target].fit(None, None)
                self.transaction.hmd['icp']['active'] = True

                # calibrate conformal estimator with validation dataset
                if self.transaction.lmd['tss']['is_timeseries'] and self.transaction.lmd['tss']['group_by']:
                    icp = self.transaction.hmd['icp'][target]
                    group_keys = icp['__group_keys']
                    for group in icp['__groups']:
                        icp_df = deepcopy(self.transaction.input_data.cached_val_df)  # validation_df)
                        icp_df[f'__predicted_{target}'] = normal_predictions[target]
                        icp[frozenset(group)].index = icp_df.columns
                        for key, val in zip(group_keys, group):
                            icp_df = icp_df[icp_df[key] == val]  # select only relevant rows
                        icp[frozenset(group)].nc_function.model.prediction_cache = icp_df.pop(f'__predicted_{target}').values
                        icp_df, y = clean_df(icp_df, target, self.transaction, is_classification, fit_params)
                        icp[frozenset(group)].calibrate(icp_df.values, y)
                else:
                    icp_df = deepcopy(self.transaction.input_data.cached_val_df)
                    icp_df, y = clean_df(icp_df, target, self.transaction, is_classification, fit_params)
                    self.transaction.hmd['icp'][target].index = icp_df.columns
                    self.transaction.hmd['icp'][target].calibrate(icp_df.values, y)

                # TODO: modify this method so that it triggers gby behavior if passed a list of icps
                conf, ranges = get_conf_range(icp_df, icp, target, typing_info, self.transaction.lmd)

                if not is_classification:
                    normal_predictions[f'{target}_confidence_range'] = ranges

                # send data to GUI
                # @TODO Limiting to 4 as to not kill the GUI, sample later (or maybe only select latest?)
                if self.transaction.lmd['tss']['is_timeseries'] and len(normal_predictions[output_columns[0]]) < pow(10, 4):
                    self.transaction.lmd['test_data_plot'][target] = {
                        'real': deepcopy(list(validation_df[target]))
                        ,'predicted': deepcopy(list(normal_predictions[target])[0:200])
                        ,'confidence': deepcopy(None if ranges is None else ranges[0:200])
                        ,'order_by': deepcopy(list(validation_df[self.transaction.lmd['tss']['order_by'][0]])[0:200])
                    }

        # get accuracy metric
        normal_accuracy = evaluate_accuracy(
            normal_predictions,
            validation_df,
            self.transaction.lmd['stats_v2'],
            output_columns,
            backend=self.transaction.model_backend
        )

        empty_input_predictions = {}
        empty_input_accuracy = {}
        empty_input_predictions_test = {}

        if not self.transaction.lmd['disable_column_importance']:
            ignorable_input_columns = [x for x in input_columns if self.transaction.lmd['stats_v2'][x]['typing']['data_type'] != DATA_TYPES.FILE_PATH
                            and (not self.transaction.lmd['tss']['is_timeseries'] or
                                 (x not in self.transaction.lmd['tss']['order_by'] and
                                 x not in self.transaction.lmd['tss']['historical_columns']))]

            for col in ignorable_input_columns:
                # TODO: if we return the already shaped df, then we could also simply pass it here and avoid approx.
                # TODO: n_col reshapings, making model analysis SO much faster, very likely worth the effort!
                empty_input_predictions[col] = self.transaction.model_backend.predict('validate', ignore_columns=[col])
                empty_input_predictions_test[col] = self.transaction.model_backend.predict('test', ignore_columns=[col])
                empty_input_accuracy[col] = evaluate_accuracy(
                    empty_input_predictions[col],
                    validation_df,
                    self.transaction.lmd['stats_v2'],
                    output_columns,
                    backend=self.transaction.model_backend
                )

            # Get some information about the importance of each column
            self.transaction.lmd['column_importances'] = {}
            for col in ignorable_input_columns:
                accuracy_increase = (normal_accuracy - empty_input_accuracy[col])
                # normalize from 0 to 10
                self.transaction.lmd['column_importances'][col] = 10 * max(0, accuracy_increase)
                assert self.transaction.lmd['column_importances'][col] <= 10

        # Get accuracy stats
        overall_accuracy_arr = []
        self.transaction.lmd['accuracy_histogram'] = {}
        self.transaction.lmd['confusion_matrices'] = {}
        self.transaction.lmd['accuracy_samples'] = {}
        self.transaction.hmd['acc_stats'] = {}

        self.transaction.lmd['train_data_accuracy'] = {}
        self.transaction.lmd['test_data_accuracy'] = {}
        self.transaction.lmd['valid_data_accuracy'] = {}

        for col in output_columns:

            # Training data accuracy
            predictions = self.transaction.model_backend.predict('predict_on_train_data')
            self.transaction.lmd['train_data_accuracy'][col] = evaluate_accuracy(
                predictions,
                self.transaction.input_data.train_df,
                self.transaction.lmd['stats_v2'],
                [col],
                backend=self.transaction.model_backend
            )

            # Testing data accuracy
            # predictions = self.transaction.model_backend.predict('test')  # TODO: check if this can be replaced
            self.transaction.lmd['test_data_accuracy'][col] = evaluate_accuracy(
                normal_predictions_test,
                test_df,
                self.transaction.lmd['stats_v2'],
                [col],
                backend=self.transaction.model_backend
            )

            # Validation data accuracy
            # predictions = self.transaction.model_backend.predict('validate')  # TODO: check if this can be replaced
            self.transaction.lmd['valid_data_accuracy'][col] = evaluate_accuracy(
                normal_predictions,
                validation_df,
                self.transaction.lmd['stats_v2'],
                [col],
                backend=self.transaction.model_backend
            )

        for col in output_columns:
            acc_stats = AccStats(
                col_stats=self.transaction.lmd['stats_v2'][col],
                col_name=col,
                input_columns=input_columns
            )

            predictions_arr = [normal_predictions_test] + [x for x in empty_input_predictions_test.values()]

            acc_stats.fit(
                test_df,
                predictions_arr,
                [[ignored_column] for ignored_column in empty_input_predictions_test]
            )

            overall_accuracy, accuracy_histogram, cm, accuracy_samples = acc_stats.get_accuracy_stats()
            overall_accuracy_arr.append(overall_accuracy)

            self.transaction.lmd['accuracy_histogram'][col] = accuracy_histogram
            self.transaction.lmd['confusion_matrices'][col] = cm
            self.transaction.lmd['accuracy_samples'][col] = accuracy_samples
            self.transaction.hmd['acc_stats'][col] = pickle_obj(acc_stats)

        self.transaction.lmd['validation_set_accuracy'] = normal_accuracy
        if self.transaction.lmd['stats_v2'][col]['typing']['data_type'] == DATA_TYPES.NUMERIC:
            self.transaction.lmd['validation_set_accuracy_r2'] = normal_accuracy
