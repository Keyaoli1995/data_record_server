import unittest

from data_record_server.protocol import FeedResult, JsonlStreamParser


class JsonlStreamParserTest(unittest.TestCase):
    def test_keeps_original_crlf_and_device_id_across_chunks(self):
        parser = JsonlStreamParser(64)

        self.assert_empty_result(parser.feed(b'{"deviceId":"D1"}\r'))
        result = parser.feed(b'\n{"deviceId":"D1","n":2}\n')

        self.assert_result(
            result,
            raw_lines=(b'{"deviceId":"D1"}\r\n', b'{"deviceId":"D1","n":2}\n'),
            device_ids=("D1", "D1"),
        )

    def test_drains_valid_invalid_valid_records_from_one_feed(self):
        parser = JsonlStreamParser(64)
        valid_line = b'{"deviceId":"D1"}\n'

        result = parser.feed(valid_line + b'{"deviceId":\n' + valid_line)

        self.assert_result(
            result,
            raw_lines=(valid_line, valid_line),
            device_ids=("D1", "D1"),
            reasons=("invalid_json",),
        )
        self.assert_empty_result(parser.finish())

    def test_drains_invalid_then_valid_record_from_one_feed(self):
        parser = JsonlStreamParser(64)
        valid_line = b'{"deviceId":"D1"}\n'

        result = parser.feed(b'{"deviceId":\n' + valid_line)

        self.assert_result(
            result,
            raw_lines=(valid_line,),
            device_ids=("D1",),
            reasons=("invalid_json",),
        )
        self.assert_empty_result(parser.finish())

    def test_preserves_multiple_rejections_in_input_order(self):
        parser = JsonlStreamParser(64)
        valid_line = b'{"deviceId":"D1"}\n'

        result = parser.feed(
            b'\xff\n'
            b'{"deviceId":\n'
            b'["D1"]\n'
            b'{"deviceId":""}\n'
            + valid_line
        )

        self.assert_result(
            result,
            raw_lines=(valid_line,),
            device_ids=("D1",),
            reasons=(
                "invalid_utf8",
                "invalid_json",
                "json_not_object",
                "missing_device_id",
            ),
        )

    def test_rejects_invalid_utf8(self):
        self.assert_rejection_is_recoverable(b'\xff\n', "invalid_utf8")

    def test_rejects_invalid_json(self):
        self.assert_rejection_is_recoverable(b'{"deviceId":\n', "invalid_json")

    def test_rejects_non_standard_json_number_constants(self):
        for constant in (b"NaN", b"Infinity", b"-Infinity"):
            with self.subTest(constant=constant):
                self.assert_rejection_is_recoverable(
                    b'{"deviceId":"D1","value":' + constant + b"}\n",
                    "invalid_json",
                )

    def test_rejects_json_that_is_not_an_object(self):
        self.assert_rejection_is_recoverable(b'["D1"]\n', "json_not_object")

    def test_rejects_record_without_a_nonempty_string_device_id(self):
        for data in (
            b"{}\n",
            b'{"deviceId":""}\n',
            b'{"deviceId":1}\n',
            b'{"deviceId":null}\n',
            b'{"deviceId":true}\n',
        ):
            with self.subTest(data=data):
                self.assert_rejection_is_recoverable(data, "missing_device_id")

    def test_rejects_device_id_change_after_first_accepted_record(self):
        parser = JsonlStreamParser(64)
        first_line = b'{"deviceId":"D1"}\n'
        changed_line = b'{"deviceId":"D2"}\n'

        result = parser.feed(first_line + changed_line + first_line)

        self.assert_result(
            result,
            raw_lines=(first_line, first_line),
            device_ids=("D1", "D1"),
            reasons=("device_id_changed",),
        )

    def test_accepts_line_at_exactly_the_limit_when_newline_arrives_later(self):
        prefix = b'{"deviceId":"D1","padding":"'
        suffix = b'"}'
        line = prefix + b"x" * (64 - len(prefix) - len(suffix)) + suffix
        parser = JsonlStreamParser(64)

        self.assertEqual(64, len(line))
        self.assert_empty_result(parser.feed(line))
        result = parser.feed(b"\n")

        self.assert_result(
            result,
            raw_lines=(line + b"\n",),
            device_ids=("D1",),
        )

    def test_rejects_unterminated_line_that_exceeds_limit_and_closes_connection(self):
        parser = JsonlStreamParser(64)

        result = parser.feed(b"x" * 65)

        self.assert_result(
            result,
            reasons=("line_too_long",),
            close_connection=True,
        )
        self.assert_empty_result(parser.feed(b""))

    def test_clears_buffer_after_complete_line_exceeds_limit(self):
        parser = JsonlStreamParser(64)
        valid_line = b'{"deviceId":"D1"}\n'

        result = parser.feed(b"x" * 65 + b"\n" + valid_line)

        self.assert_result(
            result,
            reasons=("line_too_long",),
            close_connection=True,
        )
        self.assert_empty_result(parser.feed(b""))

    def test_rejects_incomplete_line_at_end_of_stream(self):
        parser = JsonlStreamParser(64)
        self.assert_empty_result(parser.feed(b'{"deviceId":"D1"}'))

        result = parser.finish()

        self.assert_result(result, reasons=("incomplete_line",))

    def assert_rejection_is_recoverable(self, data, reason):
        parser = JsonlStreamParser(64)

        self.assert_result(parser.feed(data), reasons=(reason,))
        self.assert_accepted_d1(parser.feed(b'{"deviceId":"D1"}\n'))

    def assert_accepted_d1(self, result):
        self.assert_result(
            result,
            raw_lines=(b'{"deviceId":"D1"}\n',),
            device_ids=("D1",),
        )

    def assert_empty_result(self, result):
        self.assert_result(result)

    def assert_result(
        self,
        result,
        raw_lines=(),
        device_ids=(),
        reasons=(),
        close_connection=False,
    ):
        self.assertIsInstance(result, FeedResult)
        self.assertEqual(raw_lines, tuple(record.raw_line for record in result.records))
        self.assertEqual(device_ids, tuple(record.device_id for record in result.records))
        self.assertEqual(reasons, tuple(error.reason for error in result.rejections))
        self.assertEqual(close_connection, result.close_connection)
