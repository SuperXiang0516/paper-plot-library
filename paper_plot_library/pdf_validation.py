"""Bounded SI PDF validation; no full text is redistributed."""
import io
import re


def verify_pdf(raw, expected_doi="", expected_title="", label=""):
    if not isinstance(raw, bytes) or not raw.startswith(b"%PDF-") or len(raw) > 10000000:
        raise ValueError("Supplement is not a bounded PDF")
    try:
        from pypdf import PdfReader
    except ImportError:
        return {"status": "parser_unavailable", "page_count": None, "identityEvidence": "未安装 PDF 校验器"}
    try:
        reader = PdfReader(io.BytesIO(raw), strict=True)
        if reader.is_encrypted:
            raise ValueError("Encrypted supplement requires separate access")
        pages = len(reader.pages)
        if not 0 < pages <= 500:
            raise ValueError("Supplement page count outside bounded limit")
        chunks = []
        for page in reader.pages[:3]:
            # Avoid extracting extremely inflated streams even from small compressed files.
            content = page.get_contents()
            if content is not None and len(content.get_data()) > 2000000:
                raise ValueError("PDF page content stream too large")
            chunks.append((page.extract_text() or "")[:50000])
        text = " ".join(" ".join(chunks).casefold().split())
        title = " ".join(str(expected_title).casefold().split())
        doi = str(expected_doi).casefold()
        if doi and doi in text:
            identity = "原文 DOI 出现在前3页"
        elif len(title) >= 24 and title[:48] in text:
            identity = "论文题名片段出现在前3页"
        elif re.search(r"supplement(?:ary|al)|supporting information|reporting summary|peer review|reviewer report|supplementary figures", text):
            identity = "补充材料/报告标题标志出现；来源由精确 DOI JATS 附件及哈希绑定"
        else:
            return {"status": "identity_unconfirmed", "page_count": pages,
                    "identityEvidence": "PDF 可解析；前3页未找到论文/补充材料身份标志，需人工核查"}
        return {"status": "verified", "page_count": pages, "identityEvidence": identity}
    except Exception:
        raise ValueError("PDF 内容或页数校验未通过") from None
