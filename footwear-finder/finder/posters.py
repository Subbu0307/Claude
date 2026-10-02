"""Printable boards, one per footwear spot, each with a QR code that opens WhatsApp with "SAVE <code>".

    python -m finder.posters finder.yaml posters.html     # then open in a browser and print (A4)
"""

import html
import sys
from urllib.parse import quote

import segno

from .config import FinderConfig, load_config


def wa_link(number: str, code: str) -> str:
    return f"https://wa.me/{number}?text={quote(f'SAVE {code}')}"


def render(config: FinderConfig) -> str:
    if not config.whatsapp_number:
        raise ValueError("Set whatsapp_number in the config: the QR codes open a chat with it")
    pages = []
    for spot in config.spots.values():
        qr = segno.make(wa_link(config.whatsapp_number, spot.code), error="m")
        svg = qr.svg_inline(scale=10, border=2, dark="#000", light="#fff")
        pages.append(f"""
<section class="page">
  <div class="site">{html.escape(config.site_name)}</div>
  <div class="code">{html.escape(spot.code)}</div>
  <div class="name">{html.escape(spot.name)}</div>
  <div class="qr">{svg}</div>
  <div class="how">
    <p><b>Leaving your footwear here?</b> Scan with your phone camera, then press send in WhatsApp.<br>
       After darshan, message us and we'll show you the way back to it.</p>
    <p lang="hi"><b>चप्पल-जूते यहाँ छोड़ रहे हैं?</b> कैमरे से स्कैन करें और WhatsApp में भेजें।<br>
       दर्शन के बाद हमें संदेश भेजें, हम आपको यहाँ तक का रास्ता बताएँगे।</p>
    <p class="alt">No QR scanner? WhatsApp <b>SAVE {html.escape(spot.code)}</b> to +{config.whatsapp_number}</p>
  </div>
</section>""")
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Footwear spot boards</title>
<style>
  @page {{ size: A4; margin: 12mm; }}
  body {{ font-family: system-ui, sans-serif; margin: 0; color: #111; background: #fff; }}
  .page {{ page-break-after: always; text-align: center; padding: 8mm 0; }}
  .site {{ font-size: 20pt; }}
  .code {{ font-size: 110pt; font-weight: 800; line-height: 1; margin: 4mm 0; }}
  .name {{ font-size: 22pt; margin-bottom: 6mm; }}
  .qr svg {{ width: 85mm; height: 85mm; }}
  .how {{ font-size: 14pt; max-width: 170mm; margin: 4mm auto 0; }}
  .alt {{ font-size: 12pt; color: #444; }}
</style></head><body>{''.join(pages)}</body></html>"""


def main() -> None:
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    config = load_config(sys.argv[1])
    with open(sys.argv[2], "w", encoding="utf-8") as f:
        f.write(render(config))
    print(f"Wrote {len(config.spots)} boards to {sys.argv[2]}. Print on A4 and laminate.")


if __name__ == "__main__":
    main()
