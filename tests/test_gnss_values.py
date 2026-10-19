import unittest

from data_record_server.gnss_values import normalize_record
from data_record_server.reporting_models import valid_gps, finite_number


class NumericValuesTest(unittest.TestCase):
    def test_invalid_timestamps_remain_invalid(self):
        for value in (True, False, None, '', '-1', '1.5', '1e3', 'NaN',
                      'Infinity', '１２３', '253402300800000', '9' * 5000):
            with self.subTest(value=str(value)[:30]):
                self.assertFalse(valid_gps(normalize_record({'gpsTime': value})['gpsTime']))

    def test_nonfinite_and_malformed_coordinates_are_rejected(self):
        for value in ('NaN', 'Infinity', '-inf', '1e999', 'abc', '', '1_000', True):
            with self.subTest(value=value):
                result = normalize_record({'GNSSlat': value})
                self.assertFalse(finite_number(result['GNSSlat']))

    def test_integer_precision_and_signed_decimal_coordinates(self):
        result = normalize_record({'gpsTime': '253402300799999',
                                   'GNSSlat': ' -33.86 ', 'GNSSlon': '+151.20',
                                   'GNSShigh': '1.25e2'})
        self.assertEqual(result, {'gpsTime': 253402300799999, 'GNSSlat': -33.86,
                                  'GNSSlon': 151.2, 'GNSShigh': 125.0})
