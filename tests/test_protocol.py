import unittest

from data_record_server.protocol import JsonlStreamParser, RecordRejected


class JsonlStreamParserTest(unittest.TestCase):
    def test_keeps_original_crlf_and_device_id_across_chunks(self):
        parser = JsonlStreamParser(64)

        self.assertEqual([], parser.feed(b'{"deviceId":"D1"}\r'))
        records = parser.feed(b'\n{"deviceId":"D1","n":2}\n')

        self.assertEqual(
            [b'{"deviceId":"D1"}\r\n', b'{"deviceId":"D1","n":2}\n'],
            [record.raw_line for record in records],
        )
        self.assertEqual(["D1", "D1"], [record.device_id for record in records])

    def test_rejects_invalid_utf8(self):
        self.assert_rejection_is_recoverable(b'\xff\n', "invalid_utf8")

    def test_rejects_invalid_json(self):
        self.assert_rejection_is_recoverable(b'{"deviceId":\n', "invalid_json")

    def test_rejects_json_that_is_not_an_object(self):
        self.assert_rejection_is_recoverable(b'["D1"]\n', "json_not_object")

    def test_rejects_record_without_a_nonempty_string_device_id(self):
        for data in (
            b'{}\n',
            b'{"deviceId":""}\n',
            b'{"deviceId":1}\n',
            b'{"deviceId":null}\n',
            b'{"deviceId":true}\n',
        ):
            with self.subTest(data=data):
                self.assert_rejection_is_recoverable(data, "missing_device_id")

    def test_rejects_device_id_change_after_first_accepted_record(self):
        parser = JsonlStreamParser(64)

        parser.feed(b'{"deviceId":"D1"}\n')

        with self.assertRaises(RecordRejected) as context:
            parser.feed(b'{"deviceId":"D2"}\n')

        self.assertEqual("device_id_changed", context.exception.reason)
        self.assert_accepted_d1(parser.feed(b'{"deviceId":"D1"}\n'))

    def test_accepts_line_at_exactly_the_limit_when_newline_arrives_later(self):
        prefix = b'{"deviceId":"D1","padding":"'
        suffix = b'"}'
        line = prefix + b"x" * (64 - len(prefix) - len(suffix)) + suffix
        parser = JsonlStreamParser(64)

        self.assertEqual(64, len(line))
        self.assertEqual([], parser.feed(line))
        records = parser.feed(b"\n")

        self.assertEqual([line + b"\n"], [record.raw_line for record in records])
        self.assertEqual(["D1"], [record.device_id for record in records])

    def test_rejects_unterminated_line_that_exceeds_limit(self):
        parser = JsonlStreamParser(64)

        with self.assertRaises(RecordRejected) as context:
            parser.feed(b"x" * 65)

        self.assertEqual("line_too_long", context.exception.reason)
        self.assert_accepted_d1(parser.feed(b'{"deviceId":"D1"}\n'))

    def test_rejects_incomplete_line_at_end_of_stream(self):
        parser = JsonlStreamParser(64)
        parser.feed(b'{"deviceId":"D1"}')

        with self.assertRaises(RecordRejected) as context:
            parser.finish()

        self.assertEqual("incomplete_line", context.exception.reason)

    def assert_rejection_is_recoverable(self, data, reason):
        parser = JsonlStreamParser(64)
        with self.assertRaises(RecordRejected) as context:
            parser.feed(data)

        self.assertEqual(reason, context.exception.reason)
        self.assert_accepted_d1(parser.feed(b'{"deviceId":"D1"}\n'))

    def assert_accepted_d1(self, records):
        self.assertEqual([b'{"deviceId":"D1"}\n'], [record.raw_line for record in records])
        self.assertEqual(["D1"], [record.device_id for record in records])
