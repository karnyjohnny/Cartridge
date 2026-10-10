"""Full-size screenshot viewer.

Deliberately minimal: load a local file, scale to fit, allow copy and open-in-
Explorer. No zoom animation, no transitions — the target GPU renders in software,
and a viewer that stutters is worse than one that is plain.

The image is decoded once at open and released on close, so an 8 MB screenshot
does not stay resident while the user browses elsewhere.
"""

from __future__ import annotations

import os
from typing import Optional

from PyQt5.QtCore import Qt
from PyQt5.QtGui import QPixmap
from PyQt5.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from cartridge.artwork import images as image_tools
from cartridge.text import human_size
from cartridge.ui.widgets.common import IconButton


class ImageViewerDialog(QDialog):
    """Shows one local image file at up to its native size."""

    def __init__(self, path: str, parent=None):
        super(ImageViewerDialog, self).__init__(parent)
        self.path = path
        self.setWindowTitle("Screenshot — %s" % os.path.basename(path or ""))
        self.resize(980, 640)
        self.setMinimumSize(480, 360)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(8)

        self.info = QLabel("")
        self.info.setObjectName("SecondaryText")
        layout.addWidget(self.info)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(False)
        self.scroll.setAlignment(Qt.AlignCenter)
        self.label = QLabel()
        self.label.setAlignment(Qt.AlignCenter)
        self.scroll.setWidget(self.label)
        layout.addWidget(self.scroll, 1)

        footer = QHBoxLayout()
        self.fit_check = QPushButton("Fit to window")
        self.fit_check.setCheckable(True)
        self.fit_check.setChecked(True)
        self.fit_check.setObjectName("GhostButton")
        self.fit_check.toggled.connect(self._render)
        footer.addWidget(self.fit_check)
        footer.addStretch(1)

        reveal = IconButton("Open folder", "Show this file in the file manager")
        reveal.clicked.connect(self._on_reveal)
        footer.addWidget(reveal)

        close = QPushButton("Close")
        close.setObjectName("PrimaryButton")
        close.clicked.connect(self.accept)
        footer.addWidget(close)
        layout.addLayout(footer)

        self._pixmap: Optional[QPixmap] = None
        self._load()

    # ------------------------------------------------------------------
    def _load(self) -> None:
        validation = image_tools.validate_file(self.path)
        if not validation["ok"]:
            self.info.setText(
                "This image could not be displayed: %s"
                % (validation.get("reason") or "unknown problem")
            )
            self.label.setText("Unreadable image")
            self.label.setObjectName("CoverMissing")
            return
        pixmap = QPixmap(self.path)
        if pixmap.isNull():
            self.info.setText("The file is a valid %s but Qt could not decode it."
                              % validation.get("format"))
            return
        self._pixmap = pixmap
        self.info.setText(
            "%s · %d × %d · %s · %s"
            % (
                os.path.basename(self.path),
                validation.get("width", 0),
                validation.get("height", 0),
                human_size(validation.get("bytes")),
                validation.get("format"),
            )
        )
        self._render()

    def _render(self, *_args) -> None:
        if self._pixmap is None:
            return
        if self.fit_check.isChecked():
            target = self.scroll.viewport().size()
            scaled = self._pixmap.scaled(target, Qt.KeepAspectRatio, Qt.SmoothTransformation)
            self.label.setPixmap(scaled)
            self.label.resize(scaled.size())
        else:
            self.label.setPixmap(self._pixmap)
            self.label.resize(self._pixmap.size())

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt naming
        super(ImageViewerDialog, self).resizeEvent(event)
        if self.fit_check.isChecked():
            self._render()

    def _on_reveal(self) -> None:
        from cartridge.ui.actions import reveal_file

        _ok, message = reveal_file(self.path, self)
        if not _ok:
            self.info.setText(message)

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt naming
        # Release the decoded pixels as early as possible.
        self._pixmap = None
        self.label.clear()
        super(ImageViewerDialog, self).closeEvent(event)
