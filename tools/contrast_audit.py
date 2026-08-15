"""Measure text contrast in a real browser, on the real pages.

    pip install playwright && python tools/contrast_audit.py

Not part of the test suite: it wants a browser, and the suite is meant to run
anywhere without one. tests/test_contrast.py holds the cheap half — the
palette arithmetic — and this is the half that finds what that cannot.

The difference matters. The stylesheet's own colours can all be correct while
the page is unreadable, because the failures are products of the cascade: a
.btn-outline-light themed to near-white for the dark ground it usually sits
on, dropped inside an .alert-info that Bootstrap still draws pale blue. Two
rules, each defensible, 1.01:1 together. Nothing in either file says so.

So this serves the real application, walks every element that draws text,
finds the background actually behind it by climbing ancestors until something
is opaque, and reports every pair below WCAG AA. It found 160 on the six pages
the first time it ran.

Its own blind spot is state, and that is worth knowing before trusting it: it
sees only what the seeded data renders. The series-mode banner is switched on
below for exactly that reason — the regression that put a near-black button on
a dark alert lived on every page in the application and no default render
showed it. When you add a state that changes colours, add it to seed().
"""

import json
import os
import socket
import sys
import threading
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

#: Where the browser is. Set CHROME to override; the default is what the
#: development container ships.
CHROME = os.environ.get(
    "CHROME", "/opt/pw-browsers/chromium-1194/chrome-linux/chrome",
)

WALKER = r"""
() => {
  const parse = (c) => {
    const m = c.match(/rgba?\(([\d.]+),\s*([\d.]+),\s*([\d.]+)(?:,\s*([\d.]+))?\)/);
    if (!m) return null;
    return [ +m[1], +m[2], +m[3], m[4] === undefined ? 1 : +m[4] ];
  };
  const over = (fg, bg) => {          // fg with alpha composited onto bg
    const a = fg[3];
    return [ fg[0]*a + bg[0]*(1-a), fg[1]*a + bg[1]*(1-a), fg[2]*a + bg[2]*(1-a), 1 ];
  };
  const lum = (c) => {
    const ch = [c[0], c[1], c[2]].map(v => {
      v /= 255;
      return v <= 0.03928 ? v/12.92 : Math.pow((v+0.055)/1.055, 2.4);
    });
    return 0.2126*ch[0] + 0.7152*ch[1] + 0.0722*ch[2];
  };
  const ratio = (a, b) => {
    const l1 = lum(a), l2 = lum(b);
    const hi = Math.max(l1, l2), lo = Math.min(l1, l2);
    return (hi + 0.05) / (lo + 0.05);
  };

  // The background actually behind an element: climb until something is
  // opaque, compositing every translucent layer on the way down.
  const backdrop = (el) => {
    let layers = [];
    let node = el;
    while (node) {
      const bg = parse(getComputedStyle(node).backgroundColor);
      if (bg && bg[3] > 0) {
        layers.push(bg);
        if (bg[3] === 1) break;
      }
      node = node.parentElement;
    }
    let base = [255, 255, 255, 1];            // the canvas, if nothing is opaque
    for (let i = layers.length - 1; i >= 0; i--) base = over(layers[i], base);
    return base;
  };

  const out = [];
  for (const el of document.querySelectorAll('*')) {
    // Only elements that draw their own text, not containers of text.
    const own = Array.from(el.childNodes)
      .filter(n => n.nodeType === 3 && n.textContent.trim())
      .map(n => n.textContent.trim()).join(' ');
    if (!own) continue;

    const cs = getComputedStyle(el);
    if (cs.visibility === 'hidden' || cs.display === 'none' || +cs.opacity === 0) continue;
    const box = el.getBoundingClientRect();
    if (box.width < 1 || box.height < 1) continue;

    const fgRaw = parse(cs.color);
    if (!fgRaw) continue;
    const bg = backdrop(el);
    const fg = over(fgRaw, bg);

    const px = parseFloat(cs.fontSize);
    const weight = parseInt(cs.fontWeight, 10) || 400;
    const large = px >= 24 || (px >= 18.66 && weight >= 700);

    out.push({
      text: own.slice(0, 60),
      tag: el.tagName.toLowerCase(),
      cls: (el.className && el.className.toString ? el.className.toString() : '').slice(0, 80),
      color: cs.color,
      background: `rgb(${Math.round(bg[0])}, ${Math.round(bg[1])}, ${Math.round(bg[2])})`,
      ratio: Math.round(ratio(fg, bg) * 100) / 100,
      need: large ? 3.0 : 4.5,
      px, weight,
    });
  }
  return out;
}
"""


#: The series sheet as it looks with an answer in it: two results in the list,
#: which is the state the search step exists for and which no page load makes.
_SERIES_SHEET_WITH_RESULTS = r"""
startSeriesMode();
document.getElementById('seriesShowName').value = 'The Wire';
const box = document.getElementById('seriesShowResults');
box.className = 'list-group';
box.replaceChildren(...[
  ['The Wire', 2002, 'Baltimore drug scene, seen through the eyes of dealers and police.'],
  ['The Wire', 2007, 'A documentary about the cable under the Atlantic.'],
].map(([name, year, overview]) => {
  const button = document.createElement('button');
  button.type = 'button';
  button.className = 'list-group-item list-group-item-action bg-transparent text-start';
  const strong = document.createElement('strong');
  strong.textContent = name;
  const span = document.createElement('span');
  span.className = 'text-secondary';
  span.textContent = ' (' + year + ')';
  const div = document.createElement('div');
  div.className = 'small text-secondary';
  div.textContent = overview;
  button.append(strong, span, div);
  return button;
}));
"""


def _open_more_sheet(page):
    """The bottom bar's More sheet — a phone-only thing that has to be asked
    for, so it is skipped where the bar itself is display:none."""
    if not page.evaluate(
            "() => { const b = document.getElementById('moreNavBtn');"
            " return !!b && getComputedStyle(b).display !== 'none'; }"):
        return False
    page.evaluate("document.getElementById('moreNavBtn').click()")
    page.wait_for_timeout(600)          # it slides in
    return True


def _open_series_sheet(page):
    page.evaluate(_SERIES_SHEET_WITH_RESULTS)
    page.wait_for_timeout(600)
    return True


def _open_series_confirm(page):
    page.evaluate("startSeriesMode(); pickSeriesShow(1438, 'The Wire', 2002)")
    page.wait_for_timeout(600)
    return True


def _open_rematch_sheet(page):
    page.evaluate("openRematchModal(1, 'Jumanji')")
    page.wait_for_timeout(600)
    return True


def _open_error_modal(page):
    page.evaluate(
        "showError(1, 'The Black Cauldron (1985)',"
        " 'The encode finished with no audio at all.')")
    page.wait_for_timeout(600)
    return True


#: (label, path, prepare). prepare runs after the page has loaded and returns
#: False when the state it wants does not exist at this width.
#:
#: Everything below the plain pages is a state that only exists after somebody
#: opened something, and every one of them was unmeasured until it was listed
#: here — which is the same blind spot the series-mode banner had. When you add
#: a state that changes colours, add it here as well as to seed().
PAGES = [
    ("/", "/", None),
    ("/history", "/history", None),
    ("/settings", "/settings", None),
    ("/storage", "/storage", None),
    ("/doctor", "/doctor", None),
    ("/logs", "/logs", None),
    ("/ (more sheet)", "/", _open_more_sheet),
    ("/ (series sheet, find)", "/", _open_series_sheet),
    ("/ (series sheet, confirm)", "/", _open_series_confirm),
    ("/history (re-match sheet)", "/history", _open_rematch_sheet),
    ("/history (error detail)", "/history", _open_error_modal),
]


def seed(config):
    """Jobs covering the states whose markup differs, the banner included."""
    from adr.models import Job, JobStatus, Track, TrackStatus, get_session, init_db, utcnow

    init_db()
    session = get_session()

    # The one the user reported: a TV disc mid-rip, which is the only state
    # that renders the alert-info banner with buttons in it.
    session.add(Job(
        disc_label="SEACROW", title="Life on Seacrow Island", year=1964,
        drive="/dev/sr0", status=JobStatus.RIPPING, progress_rip=0.075,
        content_type="series", series_season=1, series_first_episode=1,
    ))
    # A film mid-rip: renders the "This is a TV disc" button instead.
    session.add(Job(
        disc_label="JUMANJI", title="Jumanji", year=1995,
        drive="/dev/sr1", status=JobStatus.ENCODING, progress_rip=1.0,
        progress_encode=0.42,
    ))
    # A failure, for alert-danger and the error text.
    session.add(Job(
        disc_label="TARAN", title="The Black Cauldron", year=1985,
        drive="/dev/sr0", status=JobStatus.ERROR,
        error_message="The encode finished with no audio at all.",
        completed_at=utcnow(),
    ))
    done = Job(
        disc_label="CHARLOTTE", title="Charlotte's Web", year=1973,
        drive="/dev/sr0", status=JobStatus.DONE, progress_rip=1.0,
        progress_encode=1.0, completed_at=utcnow(),
        output_path="/mnt/media/Charlotte's Web (1973)",
        plex_path="/mnt/media/Charlotte's Web (1973)",
    )
    session.add(done)
    session.commit()
    session.add(Track(job_id=done.id, track_number=1, filename="t00.mkv",
                      size_mb=4096.0, status=TrackStatus.DONE))
    session.commit()
    session.close()


def main():
    import tempfile

    from playwright.sync_api import sync_playwright

    from adr.config import Config
    from web.app import create_app

    root = Path(tempfile.mkdtemp())
    for name in ("raw", "completed", "staging"):
        (root / name).mkdir()
    config = Config(str(root / "adr.yaml"))
    config.update({
        "completed_path": str(root / "completed"),
        "raw_path": str(root / "raw"),
        "staging_path": str(root / "staging"),
        # The series-mode banner only exists while this is on, so no default
        # render has ever shown it — which is exactly why it went unmeasured.
        "series_mode": True,
        "series_mode_show": "Life on Seacrow Island",
        "series_mode_year": 1964,
        "series_mode_season": 1,
        "series_mode_next_episode": 5,
        "series_mode_discs": 2,
    })
    seed(config)

    app = create_app(config)
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()

    threading.Thread(
        target=lambda: app.run(host="127.0.0.1", port=port, threaded=True,
                               use_reloader=False),
        daemon=True,
    ).start()

    import time
    for _ in range(80):
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.25).close()
            break
        except OSError:
            time.sleep(0.25)

    # Phone first: it is where the report came from, and a 390px viewport
    # reflows things onto backgrounds they never share on a desktop.
    viewports = [("iphone", 390, 844), ("desktop", 1440, 900)]

    findings = []
    with sync_playwright() as pw:
        # The pinned build here is not the one this playwright expects, and
        # downloading another is both unnecessary and blocked.
        browser = pw.chromium.launch(
            executable_path=CHROME if os.path.exists(CHROME) else None,
            args=["--no-sandbox"],
        )
        for vname, width, height in viewports:
            page = browser.new_page(viewport={"width": width, "height": height})
            for label, path, prepare in PAGES:
                page.goto(f"http://127.0.0.1:{port}{path}", wait_until="networkidle")
                page.wait_for_timeout(400)
                if prepare is not None and prepare(page) is False:
                    continue
                for item in page.evaluate(WALKER):
                    if item["ratio"] < item["need"]:
                        item["page"] = label
                        item["viewport"] = vname
                        findings.append(item)
            page.close()
        browser.close()

    findings.sort(key=lambda f: f["ratio"])
    print(json.dumps(findings, indent=2))
    print(f"\n{len(findings)} contrast failure(s)", file=sys.stderr)


if __name__ == "__main__":
    main()
