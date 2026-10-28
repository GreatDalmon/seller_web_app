"""
Invoice generation (PDF and JPG) for a recorded sale.

The renderers only need an InvoiceData object, so they have no database or Django dependency.
build_invoice_data() is the one function that reads a Sale.

Fonts: the DejaVu files in seller_app/fonts have the naira sign. If they are ever missing, the
invoice still renders and writes "NGN" instead of the naira symbol.
"""
from dataclasses import dataclass, field
from decimal import Decimal
from io import BytesIO
from pathlib import Path
from xml.sax.saxutils import escape

FONT_DIR = Path(__file__).resolve().parent / 'fonts'
FONT_REGULAR = FONT_DIR / 'DejaVuSans.ttf'
FONT_BOLD = FONT_DIR / 'DejaVuSans-Bold.ttf'
HAS_FONTS = FONT_REGULAR.exists() and FONT_BOLD.exists()
CURRENCY = '₦' if HAS_FONTS else 'NGN '

GREEN = '#166534'
GREEN_LIGHT = '#ecfdf3'
GREEN_MID = '#a7f0c4'
AMBER = '#b45309'
AMBER_LIGHT = '#fef3c7'
INK = '#1c1917'
GREY = '#78716c'
RULE = '#d6d3d1'


@dataclass
class InvoiceLine:
    name: str
    quantity: int
    unit_price: Decimal
    amount: Decimal


@dataclass
class InvoiceData:
    company: str
    company_phone: str
    number: str
    date: str
    time: str
    customer: str
    customer_phone: str
    total: Decimal
    paid: Decimal
    lines: list = field(default_factory=list)

    @property
    def balance(self):
        return max(self.total - self.paid, Decimal('0'))

    @property
    def is_paid(self):
        return self.balance <= 0


def money(value):
    return f'{CURRENCY}{Decimal(value):,.2f}'


def build_invoice_data(sale, seller):
    """Collect everything the invoice shows from a Sale (date and time are in the seller's local time)."""
    from django.utils import timezone

    local = timezone.localtime(sale.date)
    lines = [
        InvoiceLine(item.product.name, item.quantity, item.price_at_sale, item.price_at_sale * item.quantity)
        for item in sale.items.all()
    ]
    return InvoiceData(
        company=seller.business_name or seller.username or 'Invoice',
        company_phone=seller.phone_number or '',
        number=f'INV-{sale.pk:05d}',
        date=local.strftime('%d %b %Y'),
        time=local.strftime('%I:%M %p').lstrip('0'),
        customer=sale.customer.name if sale.customer else 'Walk-in customer',
        customer_phone=sale.customer.phone_number if sale.customer else '',
        total=sale.total_amount,
        paid=sale.amount_paid,
        lines=lines,
    )


def file_name(data, extension):
    safe = ''.join(c if c.isalnum() else '-' for c in data.customer).strip('-')[:30] or 'customer'
    return f'{data.number}-{safe}.{extension}'


# ---------------------------------------------------------------- PDF
def render_pdf(d):
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_CENTER, TA_RIGHT
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    if HAS_FONTS:
        if 'InvSans' not in pdfmetrics.getRegisteredFontNames():
            pdfmetrics.registerFont(TTFont('InvSans', str(FONT_REGULAR)))
            pdfmetrics.registerFont(TTFont('InvSans-Bold', str(FONT_BOLD)))
        base, bold = 'InvSans', 'InvSans-Bold'
    else:
        base, bold = 'Helvetica', 'Helvetica-Bold'

    hx = colors.HexColor
    S = lambda name, **kw: ParagraphStyle(name, fontName=kw.pop('fontName', base), **kw)
    company_style = S('company', fontName=bold, fontSize=22, leading=26, textColor=colors.white)
    invoice_style = S('inv', fontName=bold, fontSize=20, leading=24, textColor=hx(GREEN))
    label_style = S('label', fontName=bold, fontSize=7.5, leading=10, textColor=hx(GREY))
    value_style = S('value', fontSize=11, leading=14, textColor=hx(INK))
    value_bold = S('valueb', fontName=bold, fontSize=12, leading=15, textColor=hx(INK))
    cell_style = S('cell', fontSize=10, leading=13, textColor=hx(INK))
    thanks_style = S('thanks', fontSize=10, leading=14, textColor=hx(GREY), alignment=TA_CENTER)
    right_label = S('rl', fontName=bold, fontSize=7.5, leading=10, textColor=hx(GREY), alignment=TA_RIGHT)
    right_value = S('rv', fontName=bold, fontSize=12, leading=15, textColor=hx(INK), alignment=TA_RIGHT)

    page_w = A4[0] - 36 * mm
    story = []

    # Header band: company name (and phone) on green
    header_text = f'{escape(d.company)}'
    if d.company_phone:
        header_text += f'<br/><font size="10" color="{GREEN_MID}">Tel: {escape(d.company_phone)}</font>'
    header = Table([[Paragraph(header_text, company_style)]], colWidths=[page_w])
    header.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, -1), hx(GREEN)),
        ('LEFTPADDING', (0, 0), (-1, -1), 14), ('RIGHTPADDING', (0, 0), (-1, -1), 14),
        ('TOPPADDING', (0, 0), (-1, -1), 16), ('BOTTOMPADDING', (0, 0), (-1, -1), 16),
    ]))
    story += [header, Spacer(1, 6 * mm)]

    # Title row
    title = Table([[Paragraph('INVOICE', invoice_style), Paragraph(escape(d.number), right_value)]],
                  colWidths=[page_w * 0.5, page_w * 0.5])
    title.setStyle(TableStyle([('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
                               ('LINEBELOW', (0, 0), (-1, -1), 0.8, hx(RULE)),
                               ('BOTTOMPADDING', (0, 0), (-1, -1), 8), ('LEFTPADDING', (0, 0), (-1, -1), 0),
                               ('RIGHTPADDING', (0, 0), (-1, -1), 0)]))
    story += [title, Spacer(1, 5 * mm)]

    # Billed to + date/time
    billed = [Paragraph('BILLED TO', label_style), Paragraph(escape(d.customer), value_bold)]
    if d.customer_phone:
        billed.append(Paragraph(escape(d.customer_phone), value_style))
    when = [Paragraph('DATE', right_label), Paragraph(escape(d.date), right_value), Spacer(1, 3),
            Paragraph('TIME', right_label), Paragraph(escape(d.time), right_value)]
    meta = Table([[billed, when]], colWidths=[page_w * 0.55, page_w * 0.45])
    meta.setStyle(TableStyle([('VALIGN', (0, 0), (-1, -1), 'TOP'),
                              ('LEFTPADDING', (0, 0), (-1, -1), 0), ('RIGHTPADDING', (0, 0), (-1, -1), 0)]))
    story += [meta, Spacer(1, 7 * mm)]

    # Items
    rows = [['#', 'ITEM', 'QTY', 'PRICE', 'AMOUNT']]
    for i, line in enumerate(d.lines, 1):
        rows.append([str(i), Paragraph(escape(line.name), cell_style), str(line.quantity),
                     money(line.unit_price), money(line.amount)])
    widths = [10 * mm, page_w - 10 * mm - 16 * mm - 34 * mm - 36 * mm, 16 * mm, 34 * mm, 36 * mm]
    items = Table(rows, colWidths=widths, repeatRows=1)
    style = [
        ('FONTNAME', (0, 0), (-1, 0), bold), ('FONTSIZE', (0, 0), (-1, 0), 8),
        ('TEXTCOLOR', (0, 0), (-1, 0), hx(GREEN)), ('BACKGROUND', (0, 0), (-1, 0), hx(GREEN_LIGHT)),
        ('FONTNAME', (0, 1), (-1, -1), base), ('FONTSIZE', (0, 1), (-1, -1), 10),
        ('TEXTCOLOR', (0, 1), (-1, -1), hx(INK)),
        ('ALIGN', (2, 0), (-1, -1), 'RIGHT'),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('TOPPADDING', (0, 0), (-1, -1), 7), ('BOTTOMPADDING', (0, 0), (-1, -1), 7),
        ('LINEBELOW', (0, 1), (-1, -1), 0.4, hx(RULE)),
        ('LINEBELOW', (0, -1), (-1, -1), 0.8, hx(GREEN)),
    ]
    items.setStyle(TableStyle(style))
    story += [items, Spacer(1, 5 * mm)]

    # Totals
    balance_color = GREEN if d.is_paid else AMBER
    balance_bg = GREEN_LIGHT if d.is_paid else AMBER_LIGHT
    totals = Table([
        ['Total', money(d.total)],
        ['Amount paid', money(d.paid)],
        ['PAID IN FULL' if d.is_paid else 'BALANCE DUE', money(d.balance)],
    ], colWidths=[40 * mm, 42 * mm], hAlign='RIGHT')
    totals.setStyle(TableStyle([
        ('FONTNAME', (0, 0), (-1, -1), base), ('FONTSIZE', (0, 0), (-1, -1), 10.5),
        ('FONTNAME', (0, 0), (-1, 0), bold),
        ('ALIGN', (1, 0), (1, -1), 'RIGHT'),
        ('TOPPADDING', (0, 0), (-1, -1), 6), ('BOTTOMPADDING', (0, 0), (-1, -1), 6),
        ('BACKGROUND', (0, 2), (-1, 2), hx(balance_bg)), ('TEXTCOLOR', (0, 2), (-1, 2), hx(balance_color)),
        ('FONTNAME', (0, 2), (-1, 2), bold), ('FONTSIZE', (0, 2), (-1, 2), 11.5),
    ]))
    story += [totals, Spacer(1, 14 * mm), Paragraph('Thank you for your patronage.', thanks_style)]

    buf = BytesIO()
    SimpleDocTemplate(
        buf, pagesize=A4, leftMargin=18 * mm, rightMargin=18 * mm, topMargin=16 * mm, bottomMargin=16 * mm,
        title=f'Invoice {d.number}', author=d.company,
    ).build(story)
    return buf.getvalue()


# ---------------------------------------------------------------- JPG
def render_jpg(d):
    from PIL import Image, ImageDraw, ImageFont

    W, M = 1000, 60
    right = W - M

    def font(size, bold=False):
        if HAS_FONTS:
            return ImageFont.truetype(str(FONT_BOLD if bold else FONT_REGULAR), size)
        return ImageFont.load_default(size=size)

    def width(text, f):
        return ImageDraw.Draw(Image.new('RGB', (1, 1))).textlength(text, font=f)

    def fit(text, f, max_w):
        """Shorten text with an ellipsis until it fits max_w."""
        if width(text, f) <= max_w:
            return text
        while text and width(text + '…', f) > max_w:
            text = text[:-1]
        return text.rstrip() + '…'

    def wrap(text, f, max_w, max_lines=2):
        words, lines, cur = text.split(), [], ''
        for w in words:
            trial = f'{cur} {w}'.strip()
            if width(trial, f) <= max_w:
                cur = trial
            else:
                if cur:
                    lines.append(cur)
                cur = w
        if cur:
            lines.append(cur)
        if len(lines) > max_lines:
            lines = lines[:max_lines]
            lines[-1] = fit(lines[-1] + ' …', f, max_w)
        return [fit(line, f, max_w) for line in lines] or ['']

    # ---- measure first so the canvas and columns fit whatever the sale contains
    name_f, num_f2, head_f = font(28), font(26), font(19, True)
    gap = 28
    amount_r = right - 16
    amt_w = max([width(money(l.amount), num_f2) for l in d.lines] + [width('AMOUNT', head_f)])
    price_w = max([width(money(l.unit_price), num_f2) for l in d.lines] + [width('PRICE', head_f)])
    qty_w = max([width(str(l.quantity), num_f2) for l in d.lines] + [width('QTY', head_f)])
    price_r = amount_r - amt_w - gap
    qty_r = price_r - price_w - gap
    item_x = M + 16
    item_w = max(qty_r - qty_w - gap - item_x, 180)
    row_lines = [wrap(l.name, name_f, item_w, max_lines=3) for l in d.lines]
    row_heights = [max(78, 34 * len(r) + 40) for r in row_lines]

    company_f = font(42, True)
    company_lines = wrap(d.company, company_f, W - 2 * M, max_lines=2)
    band_h = 34 + 52 * len(company_lines) + (42 if d.company_phone else 0) + 26

    img = Image.new('RGB', (W, band_h + 1100 + sum(row_heights)), 'white')
    g = ImageDraw.Draw(img)

    def dashed(y, color=RULE, x0=M, x1=right):
        x = x0
        while x < x1:
            g.line([(x, y), (min(x + 10, x1), y)], fill=color, width=2)
            x += 18

    # Header band
    g.rectangle([0, 0, W, band_h], fill=GREEN)
    ty = 34
    for part in company_lines:
        g.text((M, ty), part, font=company_f, fill='white')
        ty += 52
    if d.company_phone:
        g.text((M, ty + 2), f'Tel: {d.company_phone}', font=font(26), fill=GREEN_MID)

    # Title
    y = band_h + 38
    g.text((M, y), 'INVOICE', font=font(38, True), fill=GREEN)
    num_f = font(30, True)
    g.text((right - width(d.number, num_f), y + 7), d.number, font=num_f, fill=INK)
    dashed(y + 68)

    # Billed to / date / time
    y += 98
    g.text((M, y), 'BILLED TO', font=font(19, True), fill=GREY)
    g.text((M, y + 28), fit(d.customer, font(32, True), 500), font=font(32, True), fill=INK)
    if d.customer_phone:
        g.text((M, y + 74), d.customer_phone, font=font(26), fill=GREY)
    for i, (label, value) in enumerate((('DATE', d.date), ('TIME', d.time))):
        yy = y + i * 84
        lf, vf = font(19, True), font(30, True)
        g.text((right - width(label, lf), yy), label, font=lf, fill=GREY)
        g.text((right - width(value, vf), yy + 26), value, font=vf, fill=INK)

    # Table header
    y += 184
    g.rectangle([M, y, right, y + 52], fill=GREEN_LIGHT)
    g.text((item_x, y + 15), 'ITEM', font=head_f, fill=GREEN)
    for edge, label in ((qty_r, 'QTY'), (price_r, 'PRICE'), (amount_r, 'AMOUNT')):
        g.text((edge - width(label, head_f), y + 15), label, font=head_f, fill=GREEN)
    y += 52

    # Rows
    for line, name_lines, row_h in zip(d.lines, row_lines, row_heights):
        top = y + (row_h - 34 * len(name_lines)) // 2
        for j, part in enumerate(name_lines):
            g.text((item_x, top + j * 34), part, font=name_f, fill=INK)
        mid = y + (row_h - 30) // 2
        for edge, text in ((qty_r, str(line.quantity)), (price_r, money(line.unit_price)),
                           (amount_r, money(line.amount))):
            g.text((edge - width(text, num_f2), mid), text, font=num_f2, fill=INK)
        y += row_h
        dashed(y)
    g.line([(M, y), (right, y)], fill=GREEN, width=3)

    # Totals
    y += 36
    lf, vf = font(28), font(28, True)
    values_w = max(width(money(v), vf) for v in (d.total, d.paid, d.balance))
    x_label = min(520, right - 16 - values_w - 220)
    for label, value in (('Total', d.total), ('Amount paid', d.paid)):
        g.text((x_label, y), label, font=lf, fill=GREY)
        v = money(value)
        g.text((right - 16 - width(v, vf), y), v, font=vf, fill=INK)
        y += 52
    color, bg = (GREEN, GREEN_LIGHT) if d.is_paid else (AMBER, AMBER_LIGHT)
    g.rectangle([x_label - 20, y, right, y + 70], fill=bg)
    label = 'PAID IN FULL' if d.is_paid else 'BALANCE DUE'
    bf = font(28, True)
    g.text((x_label, y + 19), label, font=bf, fill=color)
    v = money(d.balance)
    g.text((right - 16 - width(v, bf), y + 19), v, font=bf, fill=color)

    # Ink-stamp status mark, tilted, on the left of the totals
    stamp_text = 'PAID' if d.is_paid else 'DUE'
    sf = font(50, True)
    sw = int(width(stamp_text, sf)) + 64
    layer = Image.new('RGBA', (sw, 104), (0, 0, 0, 0))
    ld = ImageDraw.Draw(layer)
    ld.rounded_rectangle([2, 2, sw - 3, 101], radius=14, outline=color, width=5)
    ld.text((32, 22), stamp_text, font=sf, fill=color)
    layer = layer.rotate(10, expand=True, resample=Image.BICUBIC)
    img.paste(layer, (M + 10, y - 128), layer)

    y += 70 + 56
    # Footer
    dashed(y)
    tf2 = font(26)
    thanks = 'Thank you for your patronage.'
    g.text(((W - width(thanks, tf2)) / 2, y + 28), thanks, font=tf2, fill=GREY)
    y += 28 + 34 + 40

    out = BytesIO()
    img.crop((0, 0, W, y)).save(out, 'JPEG', quality=92, optimize=True)
    return out.getvalue()
