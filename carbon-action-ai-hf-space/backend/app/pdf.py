"""A very small PDF writer (standard library only): text, lines, boxes and tables on A4."""
from __future__ import annotations

W, H = 595.0, 842.0
MARGIN = 45.0


def _esc(s: str) -> str:
    return str(s).replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


class Pdf:
    def __init__(self):
        self.pages: list[list[str]] = []
        self.new_page()

    def new_page(self):
        self.pages.append([])
        self.y = H - MARGIN

    def _ensure(self, h: float):
        if self.y - h < MARGIN:
            self.new_page()

    def text(self, x: float, y: float, s: str, size: float = 10, bold: bool = False, color=(0.08, 0.13, 0.17)):
        font = "F2" if bold else "F1"
        self.pages[-1].append(f"{color[0]} {color[1]} {color[2]} rg BT /{font} {size} Tf {x:.1f} {y:.1f} Td ({_esc(s)}) Tj ET")

    def rect(self, x: float, y: float, w: float, h: float, color=(0.12, 0.62, 0.39)):
        self.pages[-1].append(f"{color[0]} {color[1]} {color[2]} rg {x:.1f} {y:.1f} {w:.1f} {h:.1f} re f")

    def line(self, x1: float, y1: float, x2: float, y2: float, color=(0.85, 0.88, 0.9), width: float = 0.6):
        self.pages[-1].append(f"{color[0]} {color[1]} {color[2]} RG {width} w {x1:.1f} {y1:.1f} m {x2:.1f} {y2:.1f} l S")

    # flowing content ----------------------------------------------------
    def heading(self, s: str, size: float = 13):
        self._ensure(size + 26)
        self.y -= size + 10
        self.text(MARGIN, self.y, s, size, True)
        self.y -= 6

    def para(self, s: str, size: float = 9.5, color=(0.33, 0.39, 0.44)):
        per_line = int((W - 2 * MARGIN) / (size * 0.49))
        words, line = str(s).split(), ""
        for w in words + [None]:
            if w is None or len(line) + len(w) + 1 > per_line:
                self._ensure(size + 4)
                self.y -= size + 3.5
                self.text(MARGIN, self.y, line, size, color=color)
                line = ""
            if w is not None:
                line = f"{line} {w}".strip()
        self.y -= 2

    def table(self, header: list[str], rows: list[list], widths: list[float], size: float = 8.5):
        """widths are fractions of the page width. Long cells are cut to fit."""
        total = W - 2 * MARGIN
        xs = [MARGIN]
        for w in widths[:-1]:
            xs.append(xs[-1] + w * total)

        def draw(row, bold=False):
            for x, cell, w in zip(xs, row, widths):
                s, limit = str(cell), int(w * total / (size * 0.5))
                self.text(x + 2, self.y, s if len(s) <= limit else s[:max(limit - 1, 1)] + ".", size, bold)

        def head():
            self._ensure(2 * size + 14)
            self.y -= size + 6
            draw(header, True)
            self.y -= 4
            self.line(MARGIN, self.y, W - MARGIN, self.y, (0.5, 0.55, 0.6))

        head()
        for row in rows:
            if self.y - size - 8 < MARGIN:
                self.new_page()
                head()
            self.y -= size + 5
            draw(row)
            self.y -= 2.5
            self.line(MARGIN, self.y, W - MARGIN, self.y)
        self.y -= 4

    def bars(self, labels: list[str], values: list[float], height: float = 90, cap: float | None = None):
        self._ensure(height + 24)
        total, top = W - 2 * MARGIN, max(values + ([cap] if cap else []) + [1e-6])
        base = self.y - height - 6
        step = total / max(len(values), 1)
        for i, v in enumerate(values):
            h = v / top * height
            over = cap is not None and v > cap
            self.rect(MARGIN + i * step + 1, base, max(step - 2, 1), h, (0.85, 0.21, 0.21) if over else (0.48, 0.3, 0.86))
        self.line(MARGIN, base, W - MARGIN, base, (0.5, 0.55, 0.6))
        if cap:
            cy = base + cap / top * height
            self.line(MARGIN, cy, W - MARGIN, cy, (0.08, 0.13, 0.17), 0.9)
            self.text(W - MARGIN - 80, cy + 2, f"daily cap {cap:g} t", 7.5)
        every = max(len(labels) // 6, 1)
        for i in range(0, len(labels), every):
            self.text(MARGIN + i * step, base - 10, labels[i], 7.5)
        self.y = base - 16

    # output ---------------------------------------------------------------
    def bytes(self, footer: str = "") -> bytes:
        objs: list[bytes] = []
        n = len(self.pages)
        # 1 catalog, 2 page tree, 3-4 fonts, then page + content pairs
        kids = " ".join(f"{5 + 2 * i} 0 R" for i in range(n))
        objs.append(b"<< /Type /Catalog /Pages 2 0 R >>")
        objs.append(f"<< /Type /Pages /Kids [{kids}] /Count {n} >>".encode())
        objs.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>")
        objs.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold /Encoding /WinAnsiEncoding >>")
        for i, ops in enumerate(self.pages):
            foot = f"0.5 0.55 0.6 rg BT /F1 7.5 Tf {MARGIN} 25 Td ({_esc(footer)}   Page {i + 1} of {n}) Tj ET"
            stream = "\n".join(ops + [foot]).encode("cp1252", "replace")
            objs.append(f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {W:.0f} {H:.0f}] "
                        f"/Resources << /Font << /F1 3 0 R /F2 4 0 R >> >> /Contents {6 + 2 * i} 0 R >>".encode())
            objs.append(f"<< /Length {len(stream)} >>\nstream\n".encode() + stream + b"\nendstream")
        out = bytearray(b"%PDF-1.4\n")
        offsets = []
        for num, body in enumerate(objs, 1):
            offsets.append(len(out))
            out += f"{num} 0 obj\n".encode() + body + b"\nendobj\n"
        xref = len(out)
        out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode()
        for off in offsets:
            out += f"{off:010d} 00000 n \n".encode()
        out += f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
        return bytes(out)
