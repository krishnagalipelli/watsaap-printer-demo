"""Generate invoice PDFs that mimic what an Indian billing/ERP package prints.

These stand in for the client's real invoice corpus until we have it. They are
deliberately awkward: seller numbers in the letterhead and footer, GSTINs, HSN
codes, amounts and dates near the customer's number — all the things that make
naive phone extraction send an invoice to the wrong person.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import fitz

A4 = fitz.paper_rect("a4")
LEFT = 40
RIGHT_COL = 320


@dataclass
class InvoiceSpec:
    """Knobs for building one test invoice."""

    seller_name: str = "Sunrise Traders"
    seller_phone: str | None = "9845012345"        # letterhead
    seller_footer_phone: str | None = "9845012345"  # footer contact line
    seller_gstin: str = "29AABCS1429B1ZX"

    customer_name: str = "Meghana Enterprises"
    customer_phone: str | None = "9876543210"
    customer_phone_label: str = "Mobile"
    customer_gstin: str | None = "29AACCM9910C1ZQ"
    customer_block_label: str = "Bill To"

    invoice_number: str = "INV-2291"
    invoice_date: str = "12/05/2026"
    total: str = "18,450.00"

    # Extra lines dropped into the customer block, e.g. a transporter number.
    extra_customer_lines: list[str] = field(default_factory=list)
    # Extra lines in the body, e.g. bank details with a long account number.
    extra_body_lines: list[str] = field(default_factory=list)

    raster: bool = False  # render as an image, i.e. no text layer
    # Resolution of that image. Doubles as a scan-quality dial: 110+ is a clean
    # scan, 60-80 is the sort of thing a tired office scanner produces, where
    # OCR starts confusing digits.
    raster_dpi: int = 110


def build(spec: InvoiceSpec, out_path: Path) -> Path:
    doc = fitz.open()
    page = doc.new_page(width=A4.width, height=A4.height)

    def text(x: float, y: float, s: str, size: int = 9, bold: bool = False) -> None:
        page.insert_text(
            (x, y), s, fontsize=size, fontname="hebo" if bold else "helv"
        )

    # --- letterhead --------------------------------------------------------
    text(LEFT, 45, spec.seller_name, size=16, bold=True)
    text(LEFT, 60, "18/2, Industrial Layout, Bengaluru 560058")
    if spec.seller_phone:
        text(LEFT, 72, f"Ph: {spec.seller_phone}  |  info@sunrisetraders.in")
    text(LEFT, 84, f"GSTIN: {spec.seller_gstin}")

    text(230, 110, "TAX INVOICE", size=13, bold=True)

    # --- invoice meta (right column) --------------------------------------
    text(RIGHT_COL, 140, f"Invoice No: {spec.invoice_number}", bold=True)
    text(RIGHT_COL, 154, f"Invoice Date: {spec.invoice_date}")
    text(RIGHT_COL, 168, "Place of Supply: 29-Karnataka")

    # --- customer block (left column) -------------------------------------
    y = 140
    text(LEFT, y, f"{spec.customer_block_label}:", bold=True)
    y += 14
    text(LEFT, y, spec.customer_name)
    y += 12
    text(LEFT, y, "No. 44, 3rd Cross, Rajajinagar")
    y += 12
    text(LEFT, y, "Bengaluru, Karnataka - 560010")
    y += 12
    if spec.customer_gstin:
        text(LEFT, y, f"GSTIN: {spec.customer_gstin}")
        y += 12
    if spec.customer_phone:
        text(LEFT, y, f"{spec.customer_phone_label}: {spec.customer_phone}")
        y += 12
    for line in spec.extra_customer_lines:
        text(LEFT, y, line)
        y += 12

    # --- line items --------------------------------------------------------
    y = 320
    text(LEFT, y, "Sr", bold=True)
    text(LEFT + 30, y, "Description", bold=True)
    text(LEFT + 250, y, "HSN", bold=True)
    text(LEFT + 310, y, "Qty", bold=True)
    text(LEFT + 370, y, "Rate", bold=True)
    text(LEFT + 450, y, "Amount", bold=True)
    y += 16

    rows = [
        ("1", "Ceramic Floor Tile 600x600", "6907", "120", "112.00", "13,440.00"),
        ("2", "Tile Adhesive 20kg", "3214", "15", "230.00", "3,450.00"),
    ]
    for sr, desc, hsn, qty, rate, amount in rows:
        text(LEFT, y, sr)
        text(LEFT + 30, y, desc)
        text(LEFT + 250, y, hsn)
        text(LEFT + 310, y, qty)
        text(LEFT + 370, y, rate)
        text(LEFT + 450, y, amount)
        y += 14

    y += 16
    text(LEFT + 370, y, "Taxable Value")
    text(LEFT + 450, y, "16,890.00")
    y += 14
    text(LEFT + 370, y, "CGST 9%")
    text(LEFT + 450, y, "780.00")
    y += 14
    text(LEFT + 370, y, "SGST 9%")
    text(LEFT + 450, y, "780.00")
    y += 16
    text(LEFT + 370, y, "Grand Total", bold=True)
    text(LEFT + 450, y, spec.total, bold=True)

    y += 40
    for line in spec.extra_body_lines:
        text(LEFT, y, line)
        y += 12

    # --- footer ------------------------------------------------------------
    footer_y = A4.height - 60
    if spec.seller_footer_phone:
        text(
            LEFT,
            footer_y,
            f"For any queries contact us on {spec.seller_footer_phone} "
            f"or info@sunrisetraders.in",
        )
    text(LEFT, footer_y + 14, "This is a computer generated invoice.")

    out_path.parent.mkdir(parents=True, exist_ok=True)

    if spec.raster:
        # Flatten to an image so there is no text layer at all.
        pix = page.get_pixmap(dpi=spec.raster_dpi)
        flat = fitz.open()
        img_page = flat.new_page(width=A4.width, height=A4.height)
        img_page.insert_image(A4, pixmap=pix)
        flat.save(out_path)
        flat.close()
    else:
        doc.save(out_path)

    doc.close()
    return out_path
