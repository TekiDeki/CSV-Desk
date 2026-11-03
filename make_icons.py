#!/usr/bin/env python3
from pathlib import Path
from PIL import Image, ImageDraw

OUT = Path(__file__).resolve().parent

def make(size, name, maskable=False):
    im = Image.new('RGB', (size, size), '#2f6fed')
    d = ImageDraw.Draw(im)
    pad = int(size * (0.20 if maskable else 0.1875))
    right = size - pad
    radius = int(size * 0.043)
    d.rounded_rectangle((pad, pad, right, right), radius=radius, fill='#ffffff')
    line = max(3, int(size * 0.035))
    y1 = pad + int(size * 0.15625)
    y2 = pad + int(size * 0.34375)
    x1 = pad + int(size * 0.21875)
    x2 = pad + int(size * 0.4375)
    for y in (y1, y2): d.line((pad, y, right, y), fill='#2f6fed', width=line)
    for x in (x1, x2): d.line((x, pad, x, right), fill='#2f6fed', width=line)
    d.line((pad + int(size * .0625), right - int(size * .09375), pad + int(size * .15625), right - int(size * .09375)), fill='#18a77b', width=line)
    im.save(OUT / name, optimize=True)

make(192, 'icon-192.png')
make(512, 'icon-512.png')
make(512, 'icon-maskable-512.png', True)
make(180, 'apple-touch-icon.png')
print('icons written')
