#!/usr/bin/env python3
"""Render scripted, clearly-labelled *illustrative* bot chat mockups (HTML -> Chrome PNG -> GIF)."""
import copy, html, json, os, subprocess, sys, shutil, tempfile
from concurrent.futures import ThreadPoolExecutor

FONT_DIR = os.environ.get("MOCK_FONT_DIR", "file:///usr/share/fonts/vazirmatn")  # folder with Vazirmatn-Regular.ttf / -Bold.ttf (OFL)
W, H = 420, 780

CSS = """
@font-face{font-family:Vz;src:url(%(f)s/Vazirmatn-Regular.ttf);font-weight:400}
@font-face{font-family:Vz;src:url(%(f)s/Vazirmatn-Bold.ttf);font-weight:700}
*{box-sizing:border-box;margin:0;padding:0}
html,body{width:%(w)dpx;height:%(h)dpx;overflow:hidden;background:#0e1621}
body{font-family:Vz,'Noto Sans CJK JP','Noto Color Emoji','DejaVu Sans',sans-serif;font-size:14.5px;color:#e8eef5;display:flex;flex-direction:column}
.hdr{height:62px;background:#17212b;display:flex;align-items:center;padding:0 14px;gap:12px;border-bottom:1px solid #0b121a;flex:none}
.av{width:40px;height:40px;border-radius:50%%;background:linear-gradient(135deg,%(c1)s,%(c2)s);display:flex;align-items:center;justify-content:center;font-size:21px}
.nm{font-weight:700;font-size:15.5px}.st{font-size:12px;color:#6d7f8f}
.rib{flex:none;text-align:center;font-size:10.5px;letter-spacing:.4px;padding:4px 8px;background:repeating-linear-gradient(45deg,#2a2208,#2a2208 8px,#352b0a 8px,#352b0a 16px);color:#f2c94c;border-bottom:1px solid #4a3b0c}
.chat{flex:1;display:flex;flex-direction:column;justify-content:flex-end;padding:10px 10px 8px;gap:6px;overflow:hidden;
 background:radial-gradient(circle at 20%% 10%%,#14202e 0,#0e1621 60%%)}
.m{max-width:86%%;display:flex;flex-direction:column;gap:3px}
.m.b{align-self:flex-start}.m.u{align-self:flex-end}
.bub{padding:7px 11px 5px;border-radius:14px;line-height:1.5;word-break:break-word;position:relative}
.b .bub{background:#182533;border-bottom-left-radius:4px}
.u .bub{background:#2b5278;border-bottom-right-radius:4px}
.bub .t{white-space:pre-wrap;unicode-bidi:plaintext;text-align:start}
.bub code{font-family:'DejaVu Sans Mono',monospace;background:rgba(255,255,255,.09);padding:0 4px;border-radius:4px;font-size:13px;direction:ltr;unicode-bidi:isolate}
.bub a,.lnk{color:#6ab3f3}
.tm{font-size:10.5px;color:#6d8296;text-align:right;margin-top:2px;direction:ltr}
.u .tm{color:#8fb4d9}
.bub.med{padding:3px 3px 5px}.bub.med .t{padding:6px 8px 0}.bub.med .tm{padding-right:8px}
.kb{display:flex;flex-direction:column;gap:3px}.kr{display:flex;gap:3px}
.kb .k{flex:1;text-align:center;background:rgba(36,50,66,.92);color:#cfe3f7;border-radius:9px;padding:7px 4px;font-size:13px;line-height:1.3;direction:auto;unicode-bidi:plaintext}
.kb .k.on{background:%(c1)s;color:#fff;box-shadow:0 0 0 2px rgba(255,255,255,.35) inset}
.inp{flex:none;height:54px;background:#17212b;display:flex;align-items:center;padding:0 12px;gap:10px;border-top:1px solid #0b121a}
.inp .f{flex:1;height:36px;border-radius:18px;background:#0e1621;color:#53677a;display:flex;align-items:center;padding:0 14px;font-size:13.5px}
.inp .ic{font-size:20px;opacity:.55}
.thumb{height:190px;border-radius:11px;position:relative;display:flex;align-items:center;justify-content:center;overflow:hidden}
.play{width:54px;height:54px;border-radius:50%%;background:rgba(0,0,0,.45);display:flex;align-items:center;justify-content:center;color:#fff;font-size:24px;padding-left:4px}
.badge{position:absolute;left:8px;top:8px;background:rgba(0,0,0,.5);color:#fff;font-size:11px;padding:2px 8px;border-radius:9px;direction:ltr}
.badge.r{left:auto;right:8px;top:auto;bottom:8px}
.aud{display:flex;align-items:center;gap:10px;padding:6px 8px;direction:ltr}
.aud .cv{width:46px;height:46px;border-radius:10px;display:flex;align-items:center;justify-content:center;font-size:22px}
.aud .pl{width:40px;height:40px;border-radius:50%%;background:%(c1)s;display:flex;align-items:center;justify-content:center;font-size:16px;color:#fff;padding-left:3px}
.wave{display:flex;gap:2px;align-items:center;height:26px}.wave i{display:block;width:3px;background:#6ab3f3;border-radius:2px}
.chk{background-color:#fff;background-image:linear-gradient(45deg,#dde2e8 25%%,transparent 25%%,transparent 75%%,#dde2e8 75%%),linear-gradient(45deg,#dde2e8 25%%,transparent 25%%,transparent 75%%,#dde2e8 75%%);background-size:20px 20px;background-position:0 0,10px 10px}
.card{background:rgba(255,255,255,.04);border-radius:10px;padding:8px 10px;margin:2px 0}
.typing{color:#6d7f8f;font-size:12px;padding:2px 6px}
"""

def esc_keep(t):
    # allow <b>, <i>, <code>, <a>, <br> already in the text; everything else is the author's responsibility
    return t

def render_msg(m):
    side = 'u' if m['who'] == 'user' else 'b'
    med = 'med' if m.get('media') else ''
    inner = ''
    if m.get('media'):
        inner += m['media']
    if m.get('text'):
        inner += '<div class="t">%s</div>' % esc_keep(m['text'])
    inner += '<div class="tm">%s%s</div>' % (m.get('time', '10:24'), ' ✓✓' if side == 'u' else '')
    kb = ''
    if m.get('buttons'):
        rows = ''
        for r in m['buttons']:
            ks = ''
            for b in r:
                on = ' on' if (m.get('pressed') == b) else ''
                ks += '<div class="k%s">%s</div>' % (on, b)
            rows += '<div class="kr">%s</div>' % ks
        kb = '<div class="kb">%s</div>' % rows
    return '<div class="m %s"><div class="bub %s">%s</div>%s</div>' % (side, med, inner, kb)

def page(spec, msgs):
    css = CSS % dict(f=FONT_DIR, w=W, h=H, c1=spec['c1'], c2=spec['c2'])
    body = ''.join(render_msg(m) for m in msgs)
    return ('<!doctype html><meta charset="utf-8"><style>%s</style>'
            '<div class="hdr"><div class="av">%s</div><div><div class="nm">%s</div><div class="st">bot</div></div></div>'
            '<div class="rib">ILLUSTRATIVE MOCKUP · NOT A REAL CHAT · NO REAL USER DATA</div>'
            '<div class="chat">%s</div>'
            '<div class="inp"><span class="ic">📎</span><div class="f">Message</div><span class="ic">🎤</span></div>') % (
        css, spec['icon'], spec['name'], body)

def snapshots(steps):
    msgs, order, out = {}, [], []
    for s in steps:
        if 'press' in s:
            msgs[s['press']]['pressed'] = s['label']
        else:
            mid = s.get('id') or 'm%d' % len(order)
            s = dict(s); s['id'] = mid
            if mid not in msgs:
                order.append(mid)
            msgs[mid] = s
        out.append(copy.deepcopy([msgs[i] for i in order]))
    return out

def shot(args):
    html_path, png_path = args
    subprocess.run(['google-chrome', '--headless=new', '--no-sandbox', '--disable-gpu', '--hide-scrollbars',
                    '--force-device-scale-factor=2', '--virtual-time-budget=2000', '--allow-file-access-from-files',
                    '--window-size=%d,%d' % (W, H), '--screenshot=' + png_path, 'file://' + html_path],
                   check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

def build(spec, outdir):
    os.makedirs(outdir, exist_ok=True)
    tmp = tempfile.mkdtemp(prefix='mock_')
    jobs, seq, finals = [], [], []
    for si, sc in enumerate(spec['scenarios']):
        snaps = snapshots(sc['steps'])
        for fi, snap in enumerate(snaps):
            hp = os.path.join(tmp, 's%d_f%02d.html' % (si, fi)); pp = hp[:-5] + '.png'
            open(hp, 'w', encoding='utf-8').write(page(spec, snap))
            jobs.append((hp, pp))
            last = fi == len(snaps) - 1
            seq.append((pp, 2.8 if last else 1.15))
            if last:
                finals.append(pp)
    with ThreadPoolExecutor(4) as ex:
        list(ex.map(shot, jobs))
    from PIL import Image
    for i, p in enumerate(finals, 1):
        shutil.copy(p, os.path.join(outdir, 'screenshot-%d.png' % i))
    # combined strip
    ims = [Image.open(p).convert('RGB') for p in finals]
    strip = Image.new('RGB', (sum(i.width for i in ims) + 16 * (len(ims) - 1), ims[0].height), (14, 22, 33))
    x = 0
    for im in ims:
        strip.paste(im, (x, 0)); x += im.width + 16
    strip.save(os.path.join(outdir, 'screenshots.png'), optimize=True)
    # gif
    lst = os.path.join(tmp, 'list.txt')
    with open(lst, 'w') as f:
        for pp, d in seq:
            f.write("file '%s'\nduration %.2f\n" % (pp, d))
        f.write("file '%s'\n" % seq[-1][0])
    subprocess.run(['ffmpeg', '-y', '-loglevel', 'error', '-f', 'concat', '-safe', '0', '-i', lst, '-vf',
                    'fps=8,scale=360:-1:flags=lanczos,split[a][b];[a]palettegen=max_colors=96[p];[b][p]paletteuse=dither=bayer:bayer_scale=4',
                    '-loop', '0', os.path.join(outdir, 'demo.gif')], check=True)
    shutil.rmtree(tmp)

def U(t='', **k):
    d = dict(who='user', text=t); d.update(k); return d
def B(t='', **k):
    d = dict(who='bot', text=t); d.update(k); return d

# ---------- helpers for media ----------
def video(c1, c2, label, dur, size, emoji='🎬'):
    return ('<div class="thumb" style="background:linear-gradient(135deg,%s,%s)"><div style="font-size:60px;opacity:.35;position:absolute">%s</div>'
            '<div class="play">▶</div><span class="badge">%s</span><span class="badge r">%s</span></div>') % (c1, c2, emoji, dur, size)

def audio(c1, c2, title, artist, dur):
    return ('<div class="aud"><div class="cv" style="background:linear-gradient(135deg,%s,%s)">🎵</div><div style="flex:1;line-height:1.35"><b>%s</b><br><span style="color:#8aa0b4;font-size:12.5px">%s · %s</span></div></div>') % (c1, c2, title, artist, dur)

def image(c1, c2, emoji, label=''):
    return ('<div class="thumb" style="background:linear-gradient(135deg,%s,%s);height:170px"><div style="font-size:78px">%s</div><span class="badge">%s</span></div>') % (c1, c2, emoji, label)

def wave():
    hs = [8,14,10,20,12,24,16,9,18,22,11,15,7,19,13,23,10,17,9,14,20,12,8,16]
    return '<div class="aud" style="padding:6px 8px"><div class="pl">▶</div><div class="wave">%s</div><span style="color:#8aa0b4;font-size:12px">0:14</span></div>' % ''.join('<i style="height:%dpx"></i>' % h for h in hs)

def chart_svg(points, color, title, ylab, lo, hi, w=360, h=190):
    n = len(points)
    xs = [30 + i * (w - 50) / (n - 1) for i in range(n)]
    ys = [h - 28 - (p - lo) / (hi - lo) * (h - 56) for p in points]
    path = ' '.join(('M' if i == 0 else 'L') + '%.1f,%.1f' % (x, y) for i, (x, y) in enumerate(zip(xs, ys)))
    area = path + ' L%.1f,%d L%.1f,%d Z' % (xs[-1], h - 28, xs[0], h - 28)
    grid = ''.join('<line x1="30" x2="%d" y1="%d" y2="%d" stroke="#2c3b4b" stroke-width="1"/>' % (w - 20, h - 28 - k * (h - 56) / 4, h - 28 - k * (h - 56) / 4) for k in range(5))
    dots = ''.join('<circle cx="%.1f" cy="%.1f" r="3.2" fill="%s"/>' % (x, y, color) for x, y in zip(xs, ys))
    return ('<div style="background:#101a25;border-radius:11px;padding:4px"><svg width="%d" height="%d" viewBox="0 0 %d %d" style="display:block">'
            '<text x="12" y="16" fill="#9fb3c6" font-size="12" font-family="Vz,sans-serif">%s</text>%s'
            '<path d="%s" fill="%s" opacity=".14"/><path d="%s" fill="none" stroke="%s" stroke-width="2.4"/>%s'
            '<text x="30" y="%d" fill="#6d7f8f" font-size="10">W1</text><text x="%d" y="%d" fill="#6d7f8f" font-size="10" text-anchor="end">W%d</text></svg></div>') % (
        w, h, w, h, title, grid, area, color, path, color, dots, h - 10, w - 20, h - 10, n)

def sticker(bg, content, size=150):
    return ('<div class="chk" style="width:%dpx;height:%dpx;border-radius:12px;display:flex;align-items:center;justify-content:center;margin:2px">%s</div>' % (size, size, content))

if __name__ == '__main__':
    mod = __import__(sys.argv[1])
    build(mod.SPEC, sys.argv[2])
