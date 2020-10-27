import os
import unittest
import datetime
import logging
from mindsdb_native import Predictor
from mindsdb_native import F
from . import DB_CREDENTIALS


class TestPostgres(unittest.TestCase):
    def setUp(self):
        self.USER = DB_CREDENTIALS['postgres']['user']
        self.PASSWORD = DB_CREDENTIALS['postgres']['password']
        self.HOST = DB_CREDENTIALS['postgres']['host']
        self.DATABASE = DB_CREDENTIALS['postgres']['database']
        self.PORT = int(DB_CREDENTIALS['postgres']['port'])

    def test_postgres_ds(self):
        import pg8000
        from mindsdb_native.libs.data_sources.postgres_ds import PostgresDS

        con = pg8000.connect(
            database=self.DATABASE,
            user=self.USER,
            password=self.PASSWORD,
            host=self.HOST,
            port=self.PORT
        )
        cur = con.cursor()

        cur.execute('DROP TABLE IF EXISTS test_mindsdb')
        cur.execute(
            'CREATE TABLE test_mindsdb(col_1 Text, col_2 Int,  col_3 Boolean, col_4 Date, col_5 Int [])')
        for i in range(0, 200):
            dt = datetime.datetime.now() - datetime.timedelta(days=i)
            dt_str = dt.strftime('%Y-%m-%d')
            cur.execute(
                f'INSERT INTO test_mindsdb VALUES (\'String {i}\', {i}, {i % 2 == 0}, \'{dt_str}\', ARRAY [1, 2, {i}])')
        con.commit()
        con.close()

        postgres_ds = PostgresDS(
            table='test_mindsdb',
            host=self.HOST,
            user=self.USER,
            password=self.PASSWORD,
            database=self.DATABASE,
            port=self.PORT
        )
                            
        assert (len(postgres_ds._df) == 200)

        mdb = Predictor(
            name='analyse_dataset_test_predictor',
            log_level=logging.ERROR
        )
        F.analyse_dataset(from_data=postgres_ds)
