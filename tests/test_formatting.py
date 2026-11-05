import unittest

from formatting import decode_docker_logs, decode_docker_logs_prefix, human_bytes, human_duration


class FormattingTests(unittest.TestCase):
    def test_human_readable_values(self):
        self.assertEqual(human_bytes(1024), "1.0 KiB")
        self.assertEqual(human_duration(90061), "1 d 1 h 1 min 1 s")

    def test_docker_multiplexed_logs(self):
        frame = b"\x01\x00\x00\x00\x00\x00\x00\x05hello"
        self.assertEqual(decode_docker_logs(frame), "hello")
        self.assertEqual(decode_docker_logs(b"plain text"), "plain text")

    def test_partial_docker_frame_preserves_payload_not_headers(self):
        frame = b"\x01\x00\x00\x00\x00\x00\x00\x0ahello"
        self.assertEqual(decode_docker_logs_prefix(frame), "hello")
        self.assertEqual(decode_docker_logs_prefix(b"plain text"), "plain text")


if __name__ == "__main__":
    unittest.main()
