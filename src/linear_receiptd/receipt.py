"""Render a readable monochrome receipt at the printer's native resolution."""
from pathlib import Path
import re

from markdown_it import MarkdownIt
from PIL import Image, ImageDraw, ImageFont
import qrcode

WIDTH = 384
MARGIN = 20


def font(size, *, bold=False, mono=False):
    name = "LiberationMono-Regular.ttf" if mono else "LiberationSans-Bold.ttf" if bold else "LiberationSans-Regular.ttf"
    return ImageFont.truetype(str(Path(__file__).parent / "assets/fonts" / name), size)



class Receipt:
    def __init__(self):
        self.image = Image.new("L", (WIDTH, 2048), "white")
        self.draw = ImageDraw.Draw(self.image)
        self.y = 20
        self.fonts = {}

    def face(self, size, bold=False, mono=False):
        key = size, bold, mono
        if key not in self.fonts:
            self.fonts[key] = font(size, bold=bold, mono=mono)
        return self.fonts[key]

    def room(self, height):
        if height > 100_000:
            raise ValueError("Receipt is too long")
        if height >= self.image.height:
            larger = Image.new("L", (WIDTH, max(height + 100, self.image.height * 2)), "white")
            larger.paste(self.image, (0, 0))
            self.image = larger
            self.draw = ImageDraw.Draw(larger)

    def text(self, text, *, size=18, bold=False, mono=False, x=MARGIN, width=None, line_height=None):
        face = self.face(size, bold, mono)
        width = WIDTH - MARGIN - x if width is None else width
        line_height = line_height or round(size * 1.45)
        lines = []
        for paragraph in str(text).replace("\t", "    ").split("\n"):
            current = ""
            for word in re.findall(r"\S+\s*", paragraph):
                if current and self.draw.textlength(current + word.rstrip(), font=face) > width:
                    lines.append(current.rstrip())
                    current = ""
                for char in word:
                    if self.draw.textlength(current + char, font=face) > width:
                        lines.append(current.rstrip())
                        current = ""
                    current += char
            lines.append(current.rstrip())
        for line in lines:
            self.room(self.y + line_height)
            self.draw.text((x, self.y), line, font=face, fill=0, anchor="lt")
            self.y += line_height
        return lines

    def rule(self, before=14, after=16):
        self.y += before
        self.room(self.y + after)
        self.draw.line((MARGIN, self.y, WIDTH - MARGIN - 1, self.y), fill=0)
        self.y += after

    def metadata(self, name, value):
        self.text(name.upper(), size=11, bold=True, line_height=17)
        self.text(value, size=16, line_height=23)
        self.y += 8

    def markdown(self, source):
        tokens = MarkdownIt("commonmark", {"html": False}).parse(source or "")
        heading = False
        depth = 0
        item = False
        quote = 0
        for token in tokens:
            if token.type == "heading_open":
                heading = True
                self.y += 8
            elif token.type == "heading_close":
                heading = False
            elif token.type in ("bullet_list_open", "ordered_list_open"):
                depth += 1
            elif token.type in ("bullet_list_close", "ordered_list_close"):
                depth = max(0, depth - 1)
            elif token.type == "list_item_open":
                item = True
            elif token.type == "blockquote_open":
                quote += 1
            elif token.type == "blockquote_close":
                quote = max(0, quote - 1)
            elif token.type == "inline":
                content = "".join(
                    child.content if child.type in ("text", "code_inline") else
                    "\n" if child.type in ("softbreak", "hardbreak") else
                    f"[Image: {child.content or 'see issue'}]" if child.type == "image" else ""
                    for child in token.children or []
                )
                prefix = "• " if item else ""
                self.text(prefix + content, size=21 if heading else 18, bold=heading,
                          x=MARGIN + min(72, max(0, depth - 1) * 12 + quote * 12))
                self.y += 4 if item else 8
                item = False
            elif token.type in ("fence", "code_block"):
                self.y += 5
                start = self.y
                self.text(token.content.rstrip("\n"), size=15, mono=True, x=MARGIN + 12, line_height=22)
                self.draw.line((MARGIN + 2, start, MARGIN + 2, self.y - 4), fill=0, width=2)
                self.y += 9
            elif token.type == "hr":
                self.rule()


def render_receipt(issue, output, *, assignee="", header="OPAL ELECTRONICS"):
    assignee = assignee or (issue.get("assignee") or {}).get("name") or "Unassigned"
    receipt = Receipt()
    receipt.text(header, size=12, bold=True, line_height=20)
    receipt.y += 8
    identifier = issue.get("identifier") or issue.get("id") or "ISSUE"
    receipt.text(identifier, size=25, bold=True, line_height=36)
    status = issue.get("status") or issue.get("state") or ""
    if isinstance(status, dict):
        status = status.get("name", "")
    if status:
        face = receipt.face(12, True)
        label = str(status).upper()
        while receipt.draw.textlength(label, font=face) > 134:
            label = label[:-2].rstrip("…") + "…"
        label_width = min(150, receipt.draw.textlength(label, font=face) + 16)
        x = WIDTH - MARGIN - label_width
        y = 48
        receipt.draw.rectangle((x, y, WIDTH - MARGIN - 1, y + 24), outline=0)
        receipt.draw.text((x + 8, y + 6), label, font=face, fill=0, anchor="lt")
    receipt.y += 7
    receipt.text(issue.get("title") or "Untitled issue", size=28, bold=True, line_height=37)
    receipt.rule()
    project = issue.get("project")
    if isinstance(project, dict):
        project = project.get("name")
    if project:
        receipt.metadata("Project", project)
    priority = issue.get("priority", "No priority")
    if isinstance(priority, dict):
        priority = priority.get("name", "No priority")
    if isinstance(priority, int):
        priority = {0: "No priority", 1: "Urgent", 2: "High", 3: "Medium", 4: "Low"}.get(priority, "No priority")
    receipt.metadata("Priority", priority)
    if issue.get("dueDate"):
        receipt.metadata("Due", issue["dueDate"])
    labels = issue.get("labels") or []
    if isinstance(labels, dict):
        labels = labels.get("nodes", [])
    if labels:
        receipt.metadata("Labels", ", ".join(label.get("name", "") if isinstance(label, dict) else str(label) for label in labels))
    receipt.text(f"Assigned to {assignee}", size=14, line_height=20)
    receipt.rule(before=15, after=18)
    if issue.get("description"):
        receipt.text("DESCRIPTION", size=11, bold=True, line_height=24)
        receipt.markdown(issue["description"])
    else:
        receipt.text("No description provided.", size=17)
    receipt.rule(before=11, after=16)
    url = issue.get("url")
    if url:
        qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M, border=4, box_size=1)
        qr.add_data(url)
        qr.make(fit=True)
        modules = len(qr.get_matrix())
        box_size = max(2, min(4, 160 // modules))
        qr_image = qr.make_image(fill_color="black", back_color="white").convert("L")
        qr_image = qr_image.resize((modules * box_size, modules * box_size), Image.Resampling.NEAREST)
        footer_y = receipt.y
        receipt.room(footer_y + qr_image.height + 45)
        receipt.image.paste(qr_image, (WIDTH - MARGIN - qr_image.width, footer_y))
        receipt.y += 13
        footer_width = WIDTH - MARGIN * 2 - qr_image.width - 12
        receipt.text("OPEN IN LINEAR", size=11, bold=True, width=footer_width, line_height=22)
        receipt.text(identifier, size=17, bold=True, width=footer_width, line_height=30)
        receipt.text("Scan for the latest\ndetails and discussion.", size=14, width=footer_width, line_height=21)
        receipt.y = max(receipt.y, footer_y + qr_image.height)
    receipt.y += 20
    result = receipt.image.crop((0, 0, WIDTH, receipt.y)).convert("1", dither=Image.Dither.NONE)
    Path(output).parent.mkdir(parents=True, exist_ok=True)
    result.save(output)
    return result
