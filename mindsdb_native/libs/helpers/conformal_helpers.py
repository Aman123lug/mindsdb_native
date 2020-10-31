from nonconformist.base import RegressorAdapter
from nonconformist.base import ClassifierAdapter
from mindsdb_native.libs.constants.mindsdb import *

import pandas as pd
import numpy as np
from copy import deepcopy


def softmax(x, T=1, axis=0):
    e_x = np.exp((x - np.max(x))/T)
    return e_x / e_x.sum(axis=axis, keepdims=True)


def _df_from_x(x, columns):
    x = pd.DataFrame(x)
    x.columns = columns
    return x


def clean_df(df, stats, output_columns, ignored_columns):
    """
    :param stats: dict with information about every column
    :param output_columns: to be predicted
    """
    for key, value in stats.items():
        if key in df.columns and key in output_columns:
            df.pop(key)
    for col in ignored_columns:
        if col in df.columns:
            df.pop(col)
    return df


def filter_cols(columns, target, ignore):
    cols = deepcopy(columns)
    for col in [target] + ignore:
        if col in cols:
            cols.remove(col)
    return cols


class ConformalRegressorAdapter(RegressorAdapter):
    def __init__(self, model, fit_params=None):
        super(ConformalRegressorAdapter, self).__init__(model, fit_params)
        self.target = fit_params['target']
        self.columns = fit_params['all_columns']
        self.ignore_columns = fit_params['columns_to_ignore']
        self.ar = fit_params['use_previous_target']
        if self.ar:
            self.columns.append(f'previous_{self.target}')

    def fit(self, x, y):
        """
        :param x: numpy.array, shape (n_train, n_features)
        :param y: numpy.array, shape (n_train)
        We omit implementing this method as the Conformal Estimator is called once
        the MindsDB mixer has already been trained. However, it has to be called to
        setup some things in the nonconformist backend.
        """
        pass

    def predict(self, x):
        """
        :param x: numpy.array, shape (n_train, n_features). Raw data for predicting outputs.
        n_features = (|all_cols| - |ignored| - |target|)

        :return: output compatible with nonconformity function. For default
        ones, this should a numpy.array of shape (n_test) with predicted values
        """
        cols = filter_cols(self.columns, self.target, self.ignore_columns)
        x = _df_from_x(x, cols)
        predictions = self.model.predict(when_data=x)
        ys = np.array(predictions[self.target]['predictions'])
        return ys


class ConformalClassifierAdapter(ClassifierAdapter):
    def __init__(self, model, fit_params=None):
        super(ConformalClassifierAdapter, self).__init__(model, fit_params)
        self.target = fit_params['target']
        self.columns = fit_params['all_columns']
        self.ignore_columns = fit_params['columns_to_ignore']
        self.ar = fit_params['use_previous_target']
        if self.ar:
            self.columns.append(f'previous_{self.target}')

    def fit(self, x, y):
        """
        :param x: numpy.array, shape (n_train, n_features)
        :param y: numpy.array, shape (n_train)
        We omit implementing this method as the Conformal Estimator is called once
        the MindsDB mixer has already been trained. However, it has to be called to
        setup some things in the nonconformist backend.
        """
        pass

    def predict(self, x):
        """
        :param x: numpy.array, shape (n_train, n_features). Raw data for predicting outputs.
        n_features = (|all_cols| - |ignored| - |target|)

        :return: output compatible with nonconformity function. For default
        ones, this should a numpy.array of shape (n_test, n_classes) with
        class probability estimates
        """
        self.model.config['include_extra_data'] = True
        cols = filter_cols(self.columns, self.target, self.ignore_columns)
        x = _df_from_x(x, cols)
        predictions = self.model.predict(when_data=x)  # ToDo: return complete class distribution and labels from lightwood

        ys = np.array(predictions[self.target]['predictions'])
        ys = self.fit_params['one_hot_enc'].transform(ys.reshape(-1, 1))

        raw = np.array(predictions[self.target]['encoded_predictions'])
        raw_s = np.max(softmax(raw, T=0.01, axis=1), axis=1)

        return ys*raw_s.reshape(-1, 1)
