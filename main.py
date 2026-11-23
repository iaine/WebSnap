import csv
import os
import re
import sys
import time

from datetime import datetime, timezone
from urllib.parse import urlparse


def resolve_lazy_imports():
    """Workaround for Python 3.15's lazy imports.

    Python 3.15 leaves some standard-library names (for example
    concurrent.futures.ThreadPoolExecutor) as placeholders that should
    load on first use. With PySide6 imported, or inside a PyInstaller
    app, that automatic loading doesn't happen and code fails with
    "'lazy_import' object is not callable". This loads every such
    placeholder explicitly and puts the real object in its place.
    Does nothing on Python 3.14 and older.
    """
    for module in list(sys.modules.values()):
        namespace = getattr(module, "__dict__", None)
        if not isinstance(namespace, dict):
            continue
        for name, value in list(namespace.items()):
            if type(value).__name__ == "lazy_import":
                try:
                    namespace[name] = value.resolve()
                except Exception:
                    pass  # Not usable on this system; leave it.


from PySide6.QtCore import (
    QObject,
    QRect,
    QSettings,
    QSize,
    QThread,
    QUrl,
    Qt,
    Signal,
    Slot,
)

from PySide6.QtGui import (
    QDesktopServices,
    QIcon,
    QImageReader,
    QPixmap,
)

from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QSpinBox,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from playwright.sync_api import (
    sync_playwright,
    TimeoutError as PlaywrightTimeoutError,
)

resolve_lazy_imports()


# --- JavaScript helpers for capturing the whole page -----------------------

# Make lazy images load straight away, and pick the element that actually
# scrolls. Many modern sites lock <html>/<body> to the window height and
# scroll an inner container instead, so a "full page" screenshot of the
# document only shows the first screen.
JS_FIND_SCROLLER = """
() => {
    document.querySelectorAll('img[loading="lazy"]').forEach(
        img => { img.loading = 'eager'; }
    );

    const doc = document.scrollingElement || document.documentElement;
    if (doc.scrollHeight > window.innerHeight + 50) {
        window.__wsScroller = doc;
        return false;
    }

    let best = null, bestArea = 0;
    for (const el of document.querySelectorAll('body *')) {
        const style = getComputedStyle(el);
        if (!/(auto|scroll|overlay)/.test(style.overflowY)) continue;
        if (el.scrollHeight <= el.clientHeight + 50) continue;
        const area = el.clientWidth * el.clientHeight;
        if (area > bestArea) { best = el; bestArea = area; }
    }

    if (best && bestArea > 0.5 * window.innerWidth * window.innerHeight) {
        window.__wsScroller = best;
        return true;
    }

    window.__wsScroller = doc;
    return false;
}
"""

# Scroll down by most of a screen. Returns true at the bottom.
JS_SCROLL_STEP = """
() => {
    const el = window.__wsScroller;
    const before = el.scrollTop;
    el.scrollBy(0, Math.round(el.clientHeight * 0.8));
    return el.scrollTop === before
        || el.scrollTop + el.clientHeight >= el.scrollHeight - 2;
}
"""

JS_AT_BOTTOM = """
() => {
    const el = window.__wsScroller;
    return el.scrollTop + el.clientHeight >= el.scrollHeight - 2;
}
"""

JS_SCROLL_TOP = "() => { window.__wsScroller.scrollTo(0, 0); }"

# Wait (up to 5 s) for images that are still downloading, and for fonts.
JS_WAIT_FOR_IMAGES = """
async () => {
    const pending = [...document.images]
        .filter(img => !img.complete)
        .map(img => new Promise(resolve => {
            img.addEventListener('load', resolve, { once: true });
            img.addEventListener('error', resolve, { once: true });
        }));
    const timeout = new Promise(resolve => setTimeout(resolve, 5000));
    await Promise.race([Promise.all(pending), timeout]);
    if (document.fonts) {
        await Promise.race([document.fonts.ready, timeout]);
    }
}
"""

# Unlock an inner scroll container so the page grows to its full height.
JS_EXPAND_SCROLLER = """
() => {
    const unlock = el => {
        el.style.setProperty('height', 'auto', 'important');
        el.style.setProperty('max-height', 'none', 'important');
        el.style.setProperty('overflow', 'visible', 'important');
    };
    let el = window.__wsScroller;
    while (el && el !== document.documentElement) {
        unlock(el);
        el = el.parentElement;
    }
    unlock(document.documentElement);
    unlock(document.body);
}
"""

JS_PAGE_SIZE = """
() => [
    Math.max(document.documentElement.scrollWidth,
             document.body ? document.body.scrollWidth : 0),
    Math.max(document.documentElement.scrollHeight,
             document.body ? document.body.scrollHeight : 0),
]
"""

# Stop scrolling after this many steps (about 70,000 px), so endless
# "infinite scroll" pages still finish.
MAX_SCROLL_STEPS = 100


CSV_HEADER = [
    "timestamp",
    "url",
    "title",
    "status_code",
    "filename",
    "width",
    "height",
    "load_time_ms",
]



# Rough time one page takes to load, scroll and save, used for the
# run-time estimate (on top of the "wait after load" setting).
EST_LOAD_SECONDS = 4
EST_SCROLL_SECONDS = 4

INTERVAL_UNITS = [
    ("seconds", 1),
    ("minutes", 60),
    ("hours", 3600),
]


def normalise_url(text):
    """Return a usable URL for one line of input, or None to skip it."""
    text = text.strip()
    if not text or text.startswith("#"):
        return None
    if "://" in text or text.startswith(("data:", "about:", "file:")):
        return text
    return "https://" + text


def url_slug(url):
    """Short, file-name-safe label for a URL, e.g. example.com-about."""
    parsed = urlparse(url)
    label = (parsed.netloc + parsed.path).strip("/") or parsed.scheme or "page"
    label = re.sub(r"^www\.", "", label)
    label = re.sub(r"[^A-Za-z0-9.]+", "-", label).strip("-.")
    return label[:50] or "page"


def format_duration(seconds):
    """Friendly duration, e.g. '45 s', '3 min', '1 h 20 min', '2 days'."""
    seconds = int(round(seconds))
    if seconds < 60:
        return f"{seconds} s"
    minutes = round(seconds / 60)
    if minutes < 60:
        return f"{minutes} min"
    hours, minutes = divmod(minutes, 60)
    if hours < 48:
        return f"{hours} h {minutes} min" if minutes else f"{hours} h"
    days = round(hours / 24, 1)
    return f"{days:g} days"


class Worker(QObject):

    progress = Signal(int)
    status = Signal(str)
    error = Signal(str)
    captured = Signal(str)  # path of a newly saved screenshot
    finished = Signal()

    def __init__(
        self,
        urls,
        output_folder,
        interval,
        rounds,
        wait_seconds,
        scroll_page=True,
    ):
        super().__init__()

        self.urls = urls
        self.output_folder = output_folder
        self.interval = interval  # seconds from the start of one round
        self.rounds = rounds      # to the start of the next
        self.wait_seconds = wait_seconds
        self.scroll_page = scroll_page

        # A distinct file-name label per URL.
        self.slugs = []
        for url in urls:
            slug = base = url_slug(url)
            n = 2
            while slug in self.slugs:
                slug = f"{base}-{n}"
                n += 1
            self.slugs.append(slug)

        self._cancel = False

    def cancel(self):
        self._cancel = True

    def sleep(self, seconds):
        """Sleep, but wake early if cancelled."""
        end = time.time() + seconds
        while not self._cancel and time.time() < end:
            time.sleep(min(0.1, end - time.time()))

    def prepare_full_page(self, page):
        """Scroll through the page so lazy and scroll-triggered content
        loads, then make sure the whole page can be screenshotted."""

        inner_scroller = page.evaluate(JS_FIND_SCROLLER)

        if self.scroll_page:
            for _ in range(MAX_SCROLL_STEPS):
                if self._cancel:
                    return

                at_bottom = page.evaluate(JS_SCROLL_STEP)
                page.wait_for_timeout(250)

                if at_bottom:
                    # Give "load more on scroll" content a moment to
                    # appear; carry on if the page got longer.
                    page.wait_for_timeout(1000)
                    if page.evaluate(JS_AT_BOTTOM):
                        break

            page.evaluate(JS_SCROLL_TOP)
            page.wait_for_timeout(500)

        try:
            page.wait_for_load_state("networkidle", timeout=5000)
        except PlaywrightTimeoutError:
            pass  # Some sites never go quiet; that's fine.

        page.evaluate(JS_WAIT_FOR_IMAGES)

        if inner_scroller:
            page.evaluate(JS_EXPAND_SCROLLER)
            page.wait_for_timeout(300)

    def capture_one(self, browser, csv_file, url, slug, round_no):
        """Load one URL and save its screenshot. Returns False if
        cancelled part-way through."""

        page = None

        try:
            page = browser.new_page(viewport={"width": 1400, "height": 900})

            start = time.time()

            response = page.goto(
                url,
                wait_until="domcontentloaded",
                timeout=30000,
            )

            # Wait in short steps so Cancel takes effect quickly.
            waited_ms = 0
            total_ms = self.wait_seconds * 1000
            while waited_ms < total_ms and not self._cancel:
                step = min(100, total_ms - waited_ms)
                page.wait_for_timeout(step)
                waited_ms += step

            if self._cancel:
                return False

            load_time_ms = int((time.time() - start) * 1000)

            if self.scroll_page:
                self.status.emit(f"  Scrolling through {url}")
            self.prepare_full_page(page)

            if self._cancel:
                return False

            timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
            filename = f"{slug}_{round_no:04d}_{timestamp}.png"
            filepath = os.path.join(self.output_folder, filename)

            page.screenshot(path=filepath, full_page=True)

            width, height = page.evaluate(JS_PAGE_SIZE)
            status_code = response.status if response else ""

            with open(csv_file, "a", newline="", encoding="utf8") as f:
                csv.writer(f).writerow(
                    [
                        datetime.now(timezone.utc).isoformat(),
                        url,
                        page.title(),
                        status_code,
                        filename,
                        width,
                        height,
                        load_time_ms,
                    ]
                )

            self.status.emit(f"  Saved {filename}")
            self.captured.emit(filepath)

        except PlaywrightTimeoutError:
            self.error.emit(f"Timed out loading {url}")

        except Exception as e:
            detail = (str(e).strip().splitlines() or [repr(e)])[0]
            self.error.emit(f"{url}: {detail}")

        finally:
            if page is not None:
                try:
                    page.close()
                except Exception:
                    pass

        return True

    @Slot()
    def run(self):

        playwright = None
        browser = None

        try:
            os.makedirs(self.output_folder, exist_ok=True)

            csv_file = os.path.join(self.output_folder, "metadata.csv")

            if not os.path.exists(csv_file):
                with open(csv_file, "w", newline="", encoding="utf8") as f:
                    csv.writer(f).writerow(CSV_HEADER)

            self.status.emit("Starting browser...")

            try:
                playwright = sync_playwright().start()
                browser = playwright.chromium.launch(headless=True)

                # Playwright may have imported more modules by now.
                resolve_lazy_imports()

            except Exception as e:
                if getattr(sys, "frozen", False):
                    # Packaged app: the browser should be inside it.
                    hint = (
                        "This copy of WebSnap is missing its built-in "
                        "browser. Please rebuild the app."
                    )
                else:
                    hint = (
                        "Run:\n"
                        "python -m playwright install chromium"
                    )
                # Keep only the first line of Playwright's message; the
                # rest is a large box-drawing banner.
                detail = (str(e).strip().splitlines() or [repr(e)])[0]
                self.error.emit(
                    f"Could not start the browser.\n\n{hint}\n\n"
                    f"Details: {detail}"
                )
                return

            total = self.rounds * len(self.urls)
            done = 0

            for round_no in range(1, self.rounds + 1):

                round_start = time.time()

                if self.rounds > 1:
                    self.status.emit(f"Round {round_no} of {self.rounds}")

                for url, slug in zip(self.urls, self.slugs):

                    if self._cancel:
                        break

                    self.status.emit(f"Capturing {url}")

                    if not self.capture_one(
                        browser, csv_file, url, slug, round_no
                    ):
                        break

                    done += 1
                    self.progress.emit(int(done / total * 100))

                if self._cancel:
                    self.status.emit("Cancelled")
                    break

                if round_no < self.rounds:
                    remaining = self.interval - (time.time() - round_start)
                    if remaining > 0:
                        next_at = datetime.fromtimestamp(
                            time.time() + remaining
                        ).strftime("%H:%M:%S")
                        self.status.emit(
                            f"Next round at {next_at} "
                            f"(in {format_duration(remaining)})"
                        )
                        self.sleep(remaining)

            else:
                self.status.emit("All done")

        except Exception as e:
            self.error.emit(f"Unexpected error:\n{e}")

        finally:
            if browser is not None:
                try:
                    browser.close()
                except Exception:
                    pass

            if playwright is not None:
                try:
                    playwright.stop()
                except Exception:
                    pass

            self.finished.emit()


class Win(QMainWindow):

    def __init__(self):

        super().__init__()

        # Named with a leading underscore so they don't shadow
        # QObject.thread().
        self._thread = None
        self._worker = None
        self._last_capture = None

        self.settings = QSettings("WebSnap", "WebSnap")

        self.setWindowTitle("WebSnap")

        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)

        # --- Pages ---------------------------------------------------------

        pages_box = QGroupBox("Pages to capture")
        pages_layout = QVBoxLayout(pages_box)

        self.urls = QPlainTextEdit()
        self.urls.setPlaceholderText(
            "https://example.com\nhttps://another-site.com/news"
        )
        self.urls.setFixedHeight(90)
        pages_layout.addWidget(self.urls)

        hint = QLabel(
            "One web address per line. \"https://\" is added if you leave "
            "it out."
        )
        hint.setStyleSheet("color: gray;")
        hint.setWordWrap(True)
        pages_layout.addWidget(hint)

        layout.addWidget(pages_box)

        # --- Schedule ------------------------------------------------------

        schedule_box = QGroupBox("Schedule")
        form = QFormLayout(schedule_box)

        self.rounds = QSpinBox()
        self.rounds.setRange(1, 100000)
        self.rounds.setToolTip(
            "How many times to capture every page. 1 = capture once."
        )
        form.addRow("Capture each page:", self._with_suffix(
            self.rounds, "time(s)"
        ))

        self.interval = QSpinBox()
        self.interval.setRange(1, 100000)
        self.interval_unit = QComboBox()
        for name, _ in INTERVAL_UNITS:
            self.interval_unit.addItem(name)
        self.interval.setToolTip(
            "How often a new round starts. If a round takes longer than "
            "this, the next one starts straight away."
        )
        interval_row = QHBoxLayout()
        interval_row.addWidget(self.interval)
        interval_row.addWidget(self.interval_unit)
        interval_row.addStretch()
        self.interval_label = QLabel("Repeat every:")
        form.addRow(self.interval_label, interval_row)

        self.wait = QSpinBox()
        self.wait.setRange(0, 300)
        self.wait.setSuffix(" s")
        self.wait.setToolTip(
            "Extra time to let each page settle (animations, pop-ups, "
            "late-loading content) before the screenshot."
        )
        form.addRow("Wait after page loads:", self._with_suffix(
            self.wait, ""
        ))

        self.scroll_page = QCheckBox(
            "Scroll through each page first (loads lazy and animated content)"
        )
        form.addRow("", self.scroll_page)

        self.estimate = QLabel()
        self.estimate.setStyleSheet("color: gray;")
        self.estimate.setWordWrap(True)
        form.addRow("", self.estimate)

        layout.addWidget(schedule_box)

        # --- Output --------------------------------------------------------

        output_box = QGroupBox("Save screenshots to")
        output_row = QHBoxLayout(output_box)

        self.out = QLineEdit()
        output_row.addWidget(self.out)

        self.browse = QPushButton("Browse…")
        self.browse.clicked.connect(self.choose_folder)
        output_row.addWidget(self.browse)

        layout.addWidget(output_box)

        # The settings above are disabled while a run is in progress.
        self.settings_widgets = [pages_box, schedule_box, output_box]

        # --- Progress and results ------------------------------------------

        self.pb = QProgressBar()
        layout.addWidget(self.pb)

        results_row = QHBoxLayout()

        thumb_col = QVBoxLayout()
        self.thumb = QPushButton("No captures yet")
        self.thumb.setFixedSize(QSize(250, 165))
        self.thumb.setIconSize(QSize(240, 155))
        self.thumb.setToolTip("Click to open the latest screenshot")
        self.thumb.setEnabled(False)
        self.thumb.clicked.connect(self.open_last_capture)
        thumb_col.addWidget(self.thumb)
        self.thumb_caption = QLabel("")
        self.thumb_caption.setStyleSheet("color: gray;")
        self.thumb_caption.setWordWrap(True)
        self.thumb_caption.setFixedWidth(250)
        thumb_col.addWidget(self.thumb_caption)
        thumb_col.addStretch()
        results_row.addLayout(thumb_col)

        self.log = QTextEdit()
        self.log.setReadOnly(True)
        results_row.addWidget(self.log)

        layout.addLayout(results_row, stretch=1)

        # --- Buttons -------------------------------------------------------

        buttons = QHBoxLayout()

        self.startb = QPushButton("Start Capture")
        self.startb.setDefault(True)
        self.cancelb = QPushButton("Cancel")
        self.cancelb.setEnabled(False)
        self.openb = QPushButton(
            "Show in Finder" if sys.platform == "darwin"
            else "Open Folder"
        )

        buttons.addWidget(self.startb)
        buttons.addWidget(self.cancelb)
        buttons.addStretch()
        buttons.addWidget(self.openb)
        layout.addLayout(buttons)

        self.startb.clicked.connect(self.start)
        self.cancelb.clicked.connect(self.cancel)
        self.openb.clicked.connect(self.open_folder)

        # Keep the estimate up to date.
        self.urls.textChanged.connect(self.update_estimate)
        self.rounds.valueChanged.connect(self.update_estimate)
        self.interval.valueChanged.connect(self.update_estimate)
        self.interval_unit.currentIndexChanged.connect(self.update_estimate)
        self.wait.valueChanged.connect(self.update_estimate)
        self.scroll_page.toggled.connect(self.update_estimate)

        self.load_settings()
        self.update_estimate()

    @staticmethod
    def _with_suffix(widget, text):
        row = QHBoxLayout()
        row.addWidget(widget)
        if text:
            row.addWidget(QLabel(text))
        row.addStretch()
        return row

    # --- Settings ----------------------------------------------------------

    def load_settings(self):
        s = self.settings
        self.urls.setPlainText(s.value("urls", "", type=str))
        self.out.setText(s.value(
            "output_folder",
            os.path.join(os.path.expanduser("~"), "WebSnap_Output"),
            type=str,
        ))
        self.rounds.setValue(s.value("rounds", 1, type=int))
        self.interval.setValue(s.value("interval_value", 5, type=int))
        unit = s.value("interval_unit", 0, type=int)
        if 0 <= unit < len(INTERVAL_UNITS):
            self.interval_unit.setCurrentIndex(unit)
        self.wait.setValue(s.value("wait", 3, type=int))
        self.scroll_page.setChecked(s.value("scroll_page", True, type=bool))

        geometry = s.value("geometry")
        if geometry is not None:
            self.restoreGeometry(geometry)
        else:
            self.resize(760, 760)

    def save_settings(self):
        s = self.settings
        s.setValue("urls", self.urls.toPlainText())
        s.setValue("output_folder", self.out.text())
        s.setValue("rounds", self.rounds.value())
        s.setValue("interval_value", self.interval.value())
        s.setValue("interval_unit", self.interval_unit.currentIndex())
        s.setValue("wait", self.wait.value())
        s.setValue("scroll_page", self.scroll_page.isChecked())
        s.setValue("geometry", self.saveGeometry())
        s.sync()

    # --- Helpers -----------------------------------------------------------

    def url_list(self):
        urls = []
        for line in self.urls.toPlainText().splitlines():
            url = normalise_url(line)
            if url and url not in urls:
                urls.append(url)
        return urls

    def interval_seconds(self):
        _, factor = INTERVAL_UNITS[self.interval_unit.currentIndex()]
        return self.interval.value() * factor

    def output_folder(self):
        return os.path.expanduser(self.out.text().strip())

    def update_estimate(self):
        urls = self.url_list()
        rounds = self.rounds.value()

        repeating = rounds > 1
        self.interval.setEnabled(repeating)
        self.interval_unit.setEnabled(repeating)
        self.interval_label.setEnabled(repeating)

        if not urls:
            self.estimate.setText("Add at least one page to capture.")
            return

        per_page = EST_LOAD_SECONDS + self.wait.value()
        if self.scroll_page.isChecked():
            per_page += EST_SCROLL_SECONDS
        per_round = per_page * len(urls)

        total = per_round
        if repeating:
            total += (rounds - 1) * max(self.interval_seconds(), per_round)

        pages = f"{len(urls)} page" + ("s" if len(urls) != 1 else "")
        shots = rounds * len(urls)
        text = (
            f"{pages} × {rounds} = {shots} screenshot"
            + ("s" if shots != 1 else "")
            + f". Estimated time: about {format_duration(total)}."
        )
        if repeating and self.interval_seconds() < per_round:
            text += (
                " (Each round takes longer than the repeat time, so rounds "
                "will run back to back.)"
            )
        self.estimate.setText(text)

    def set_running(self, running):
        for widget in self.settings_widgets:
            widget.setEnabled(not running)
        self.startb.setEnabled(not running)
        self.cancelb.setEnabled(running)

    def write_log(self, message):
        self.log.append(message)

    # --- Actions -----------------------------------------------------------

    def choose_folder(self):
        start_dir = self.output_folder()
        if not os.path.isdir(start_dir):
            start_dir = os.path.expanduser("~")
        folder = QFileDialog.getExistingDirectory(
            self, "Choose where to save screenshots", start_dir
        )
        if folder:
            self.out.setText(folder)

    def open_folder(self):
        folder = self.output_folder()
        if not os.path.isdir(folder):
            self.write_log(
                "The output folder doesn't exist yet. It will be created "
                "when you start a capture."
            )
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(folder))

    def open_last_capture(self):
        if self._last_capture and os.path.exists(self._last_capture):
            QDesktopServices.openUrl(QUrl.fromLocalFile(self._last_capture))

    def show_capture(self, path):
        """Show the top of the newest screenshot as a thumbnail."""
        self._last_capture = path

        # Full-page screenshots can be very tall, so read only the top
        # part (one screen's worth) rather than the whole image.
        reader = QImageReader(path)
        size = reader.size()
        if size.isValid() and size.width() > 0:
            top = min(size.height(), int(size.width() * 155 / 240))
            reader.setClipRect(QRect(0, 0, size.width(), top))
        image = reader.read()
        if image.isNull():
            return

        pixmap = QPixmap.fromImage(image).scaled(
            self.thumb.iconSize(),
            Qt.KeepAspectRatio,
            Qt.SmoothTransformation,
        )
        self.thumb.setText("")
        self.thumb.setIcon(QIcon(pixmap))
        self.thumb.setEnabled(True)
        self.thumb_caption.setText(os.path.basename(path))

    def capture_error(self, message):
        self.write_log(f"ERROR: {message}")

    def capture_finished(self):
        self.set_running(False)

    def thread_stopped(self):
        # Only now is it safe to let go of the QThread.
        self._worker = None
        self._thread = None

    def start(self):

        if self._thread is not None:
            return

        urls = self.url_list()
        if not urls:
            self.write_log("Please enter at least one web address.")
            self.urls.setFocus()
            return

        folder = self.output_folder()
        if not folder:
            self.write_log("Please choose a folder to save screenshots in.")
            return

        self.save_settings()

        self.pb.setValue(0)
        self.log.clear()
        self.set_running(True)

        self._thread = QThread()

        self._worker = Worker(
            urls,
            folder,
            self.interval_seconds(),
            self.rounds.value(),
            self.wait.value(),
            self.scroll_page.isChecked(),
        )

        self._worker.moveToThread(self._thread)

        self._thread.started.connect(self._worker.run)

        # Cross-thread signals are queued automatically.
        self._worker.progress.connect(self.pb.setValue)
        self._worker.status.connect(self.write_log)
        self._worker.error.connect(self.capture_error)
        self._worker.captured.connect(self.show_capture)
        self._worker.finished.connect(self.capture_finished)

        self._worker.finished.connect(self._thread.quit)
        self._worker.finished.connect(self._worker.deleteLater)

        self._thread.finished.connect(self.thread_stopped)
        self._thread.finished.connect(self._thread.deleteLater)

        self._thread.start()

    def cancel(self):
        if self._worker is not None:
            self.write_log("Cancelling...")
            self._worker.cancel()

    def closeEvent(self, event):
        self.save_settings()
        # Never let the window (and the QThread) be destroyed while
        # the capture thread is still running.
        if self._thread is not None:
            self.write_log("Stopping capture before closing...")
            self._worker.cancel()
            self._thread.quit()
            # Returns once the current page load finishes or times out
            # (at most about 30 seconds).
            self._thread.wait()
        super().closeEvent(event)


if __name__ == "__main__":

    app = QApplication(sys.argv)
    app.setApplicationName("WebSnap")

    window = Win()
    window.show()

    sys.exit(app.exec())
