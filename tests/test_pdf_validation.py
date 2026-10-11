import io
import unittest
from pypdf import PdfWriter

from paper_plot_library.pdf_validation import verify_pdf


class PdfValidationTests(unittest.TestCase):
    def test_extension_or_magic_alone_not_valid(self):
        for raw in (b"<html>PDF error</html>", b"%PDF-1.4\nnot a valid PDF"):
            with self.assertRaises(ValueError):
                verify_pdf(raw)

    def test_valid_pdf_without_identity_is_unconfirmed_not_verified(self):
        writer = PdfWriter()
        writer.add_blank_page(width=300, height=300)
        output = io.BytesIO()
        writer.write(output)
        result = verify_pdf(output.getvalue(), expected_doi="10.1234/fixture", expected_title="A synthetic paper title for testing")
        self.assertEqual(result["status"], "identity_unconfirmed")
        self.assertEqual(result["page_count"], 1)
        self.assertNotIn("text", result)

    def test_empty_pdf_and_encryption_rejected(self):
        for encrypted in (False, True):
            writer = PdfWriter()
            if encrypted:
                writer.add_blank_page(width=300, height=300)
                writer.encrypt("fixture-only")
            output = io.BytesIO()
            writer.write(output)
            with self.assertRaises(ValueError):
                verify_pdf(output.getvalue())


if __name__ == "__main__":
    unittest.main()
