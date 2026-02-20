from __future__ import annotations

import sys
import os
import cv2
import shutil
import json
import traceback
import faulthandler
import logging
from dataclasses import dataclass
from threading import Lock
from typing import Dict, List, Optional, Tuple, Literal

import numpy as np

from PySide6.QtWidgets import (
    QApplication, QWidget, QPushButton, QVBoxLayout, QLabel, QListWidget,
    QFileDialog, QHBoxLayout, QSlider, QMessageBox, QListWidgetItem,
    QComboBox, QCheckBox, QSizePolicy, QDialog, QColorDialog, QSpinBox
)
from PySide6.QtGui import QPixmap, QImage, QIcon, QKeySequence, QShortcut, QColor
from PySide6.QtCore import Qt, QTimer, QThread, Signal, QSize

from pygrabber.dshow_graph import FilterGraph


# -----------------------------
# Crash logging
# -----------------------------
faulthandler.enable(open("faultlog.txt", "w"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("app.log", encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("stopmotion")


# -----------------------------
# Undo/Redo actions
# -----------------------------
ActionType = Literal["add", "delete"]


@dataclass
class Action:
    type: ActionType
    path: str
    index: int


class UndoManager:
    def __init__(self) -> None:
        self._undo: List[Action] = []
        self._redo: List[Action] = []

    def clear(self) -> None:
        self._undo.clear()
        self._redo.clear()

    def push(self, action: Action) -> None:
        # Any new action clears redo history (standard behavior)
        self._undo.append(action)
        self._redo.clear()

    def pop_undo(self) -> Optional[Action]:
        if not self._undo:
            return None
        action = self._undo.pop()
        self._redo.append(action)
        return action

    def pop_redo(self) -> Optional[Action]:
        if not self._redo:
            return None
        action = self._redo.pop()
        self._undo.append(action)
        return action


# -----------------------------
# Project store with Trash
# -----------------------------
class ProjectStore:
    META_FILE = "project_meta.json"
    TRASH_DIR = ".trash"

    def __init__(self) -> None:
        self.project_path: str = ""
        self.frames: List[str] = []
        self.unsaved_changes: bool = False
        # map original_path -> trashed_path (so restore returns exact file)
        self._trashed: Dict[str, str] = {}

    def has_project(self) -> bool:
        return bool(self.project_path)

    def reset(self, project_path: str) -> None:
        self.project_path = project_path
        self.frames.clear()
        self._trashed.clear()
        self.unsaved_changes = False

        # Keep your old .undo_cache folder behavior
        undo_cache_dir = os.path.join(self.project_path, ".undo_cache")
        if os.path.exists(undo_cache_dir):
            shutil.rmtree(undo_cache_dir, ignore_errors=True)
        os.makedirs(undo_cache_dir, exist_ok=True)

        # Ensure trash exists
        self.ensure_trash_dir()

    def trash_dir(self) -> str:
        return os.path.join(self.project_path, self.TRASH_DIR)

    def ensure_trash_dir(self) -> None:
        if self.project_path:
            os.makedirs(self.trash_dir(), exist_ok=True)

    def _next_frame_filename(self) -> str:
        # Preserve original naming behavior for capture
        return f"frame_{len(self.frames):04d}.png"

    def capture_from_bgr(self, frame_bgr: np.ndarray) -> Tuple[int, str]:
        if not self.project_path:
            raise RuntimeError("No project path set")

        name = self._next_frame_filename()
        path = os.path.join(self.project_path, name)
        cv2.imwrite(path, frame_bgr)

        index = len(self.frames)
        self.frames.append(path)
        self.unsaved_changes = True
        return index, path

    def duplicate_frame(self, index: int) -> Tuple[int, str]:
        if not self.project_path:
            raise RuntimeError("No project path set")
        if index < 0 or index >= len(self.frames):
            raise IndexError("Frame index out of range")

        original_path = self.frames[index]
        if not os.path.exists(original_path):
            raise FileNotFoundError(original_path)

        new_name = self._next_frame_filename()
        new_path = os.path.join(self.project_path, new_name)
        shutil.copy(original_path, new_path)

        insert_at = index + 1
        self.frames.insert(insert_at, new_path)
        self.unsaved_changes = True
        return insert_at, new_path

    def _unique_trash_path(self, original_path: str) -> str:
        self.ensure_trash_dir()
        base = os.path.basename(original_path)
        dst = os.path.join(self.trash_dir(), base)

        if not os.path.exists(dst):
            return dst

        root, ext = os.path.splitext(dst)
        i = 1
        while os.path.exists(f"{root}_{i}{ext}"):
            i += 1
        return f"{root}_{i}{ext}"

    def soft_delete(self, original_path: str) -> None:
        """
        Move file into .trash instead of deleting.
        """
        if not self.project_path:
            return
        if not os.path.exists(original_path):
            return

        dst = self._unique_trash_path(original_path)
        try:
            shutil.move(original_path, dst)
            self._trashed[original_path] = dst
        except Exception as e:
            log.warning("Failed to move to trash %s -> %s (%s)", original_path, dst, e)

    def restore_from_trash(self, original_path: str) -> None:
        """
        Bring a file back from .trash to its original path.
        If we don't have a mapping, try best-effort by filename match.
        """
        if not self.project_path:
            return

        trashed = self._trashed.get(original_path)
        if trashed and os.path.exists(trashed):
            try:
                os.makedirs(os.path.dirname(original_path), exist_ok=True)
                shutil.move(trashed, original_path)
                del self._trashed[original_path]
            except Exception as e:
                log.warning("Failed to restore from trash %s -> %s (%s)", trashed, original_path, e)
            return

        # best effort: find same filename in trash
        base = os.path.basename(original_path)
        candidate = os.path.join(self.trash_dir(), base)
        if os.path.exists(candidate):
            try:
                shutil.move(candidate, original_path)
            except Exception as e:
                log.warning("Failed to restore best-effort %s -> %s (%s)", candidate, original_path, e)

    def purge_trash(self) -> None:
        """
        Permanently delete trash folder (called on app close).
        """
        tdir = self.trash_dir()
        if os.path.exists(tdir):
            shutil.rmtree(tdir, ignore_errors=True)
        self._trashed.clear()

    def delete_frame(self, index: int) -> Action:
        """
        Remove from timeline and soft-delete file to trash.
        """
        if index < 0 or index >= len(self.frames):
            raise IndexError("Frame index out of range")

        path = self.frames.pop(index)
        self.soft_delete(path)
        self.unsaved_changes = True
        return Action(type="delete", path=path, index=index)

    def restore_deleted(self, action: Action) -> None:
        """
        Undo(delete): restore file + reinsert.
        """
        self.restore_from_trash(action.path)
        idx = max(0, min(action.index, len(self.frames)))
        self.frames.insert(idx, action.path)
        self.unsaved_changes = True

    def remove_added(self, action: Action) -> None:
        """
        Undo(add): remove from list + soft-delete.
        """
        if action.path in self.frames:
            self.frames.remove(action.path)
        self.soft_delete(action.path)
        self.unsaved_changes = True

    def reapply_add(self, action: Action) -> None:
        """
        Redo(add): restore file + reinsert.
        """
        self.restore_from_trash(action.path)
        idx = max(0, min(action.index, len(self.frames)))
        if action.path not in self.frames:
            self.frames.insert(idx, action.path)
        self.unsaved_changes = True

    def reapply_delete(self, action: Action) -> None:
        """
        Redo(delete): remove from list + soft-delete again.
        """
        if action.path in self.frames:
            self.frames.remove(action.path)
        self.soft_delete(action.path)
        self.unsaved_changes = True

    def load_frames_from_disk(self) -> None:
        """
        Load frames from disk (ignores .trash).
        """
        if not self.project_path:
            return

        frames: List[str] = []
        folder = self.project_path
        for file in sorted(os.listdir(folder)):
            if file.endswith(".png") and file.startswith("frame_"):
                full_path = os.path.join(folder, file)
                if os.path.exists(full_path) and cv2.imread(full_path) is not None:
                    frames.append(full_path)
                else:
                    log.info("Skipping missing or unreadable file: %s", full_path)
        self.frames = frames
        self.ensure_trash_dir()

    def save_metadata(self, metadata: dict) -> None:
        if not self.project_path:
            return
        meta_path = os.path.join(self.project_path, self.META_FILE)
        try:
            with open(meta_path, "w", encoding="utf-8") as f:
                json.dump(metadata, f, indent=2)
        except Exception as e:
            log.warning("Failed to save metadata: %s", e)

    def load_metadata(self) -> dict:
        if not self.project_path:
            return {}
        meta_path = os.path.join(self.project_path, self.META_FILE)
        if not os.path.exists(meta_path):
            return {}
        try:
            with open(meta_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            log.warning("Failed to load metadata: %s", e)
            return {}


# -----------------------------
# Camera threads + service
# -----------------------------
class CameraSearchDialog(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent, Qt.Window | Qt.WindowTitleHint | Qt.CustomizeWindowHint)
        self.setWindowTitle("Please wait")
        self.setFixedSize(200, 80)
        self.setWindowModality(Qt.ApplicationModal)

        layout = QVBoxLayout()
        label = QLabel("Hunting down cameras...")
        label.setAlignment(Qt.AlignCenter)
        layout.addWidget(label)
        self.setLayout(layout)


class CameraSearchThread(QThread):
    cameras_found = Signal(list)  # list[(index, name)]

    def run(self):
        graph = FilterGraph()
        device_names = graph.get_input_devices()
        found: List[Tuple[int, str]] = []

        for i, name in enumerate(device_names):
            cap = cv2.VideoCapture(i)
            try:
                if cap.isOpened():
                    found.append((i, name))
            finally:
                cap.release()

        self.cameras_found.emit(found)


class CameraOpenThread(QThread):
    camera_opened = Signal(bool, int, object)  # success, index, cap-or-None

    def __init__(self, index: int):
        super().__init__()
        self.index = index

    def run(self):
        try:
            if sys.platform.startswith("win"):
                cap = cv2.VideoCapture(self.index, cv2.CAP_DSHOW)
            else:
                cap = cv2.VideoCapture(self.index)
        except Exception:
            cap = cv2.VideoCapture(self.index)

        success = cap.isOpened()
        if not success:
            try:
                cap.release()
            except Exception:
                pass
            cap = None

        self.camera_opened.emit(success, self.index, cap)


class CameraService:
    """
    Single owner of cv2.VideoCapture.
    """
    def __init__(self) -> None:
        self._cap: Optional[cv2.VideoCapture] = None
        self._lock = Lock()

    def set_cap(self, cap: Optional[cv2.VideoCapture]) -> None:
        old: Optional[cv2.VideoCapture]
        with self._lock:
            old = self._cap
            self._cap = cap
        if old:
            try:
                old.release()
            except Exception:
                pass

    def is_open(self) -> bool:
        with self._lock:
            return bool(self._cap and self._cap.isOpened())

    def read(self) -> Tuple[bool, Optional[np.ndarray]]:
        with self._lock:
            cap = self._cap
            if not cap or not cap.isOpened():
                return False, None
            ret, frame = cap.read()
            if not ret or frame is None:
                return False, None
            return True, frame

    def release(self) -> None:
        self.set_cap(None)


# -----------------------------
# Theme editor
# -----------------------------
class ThemeEditorDialog(QDialog):
    def __init__(self, parent=None, initial_theme=None):
        super().__init__(parent)
        self.setWindowTitle("Custom Theme Editor")
        self.theme = initial_theme or {}

        layout = QVBoxLayout()

        self.color_buttons = {}
        self.color_fields = {
            "bg_color": "Background Color",
            "text_color": "Text Color",
            "button_bg": "Button Background",
            "button_text": "Button Text"
        }

        for key, label_text in self.color_fields.items():
            hbox = QHBoxLayout()
            label = QLabel(label_text)
            btn = QPushButton("Choose...")
            btn.clicked.connect(lambda _, k=key: self.pick_color(k))
            color_display = QLabel()
            color_display.setFixedSize(60, 20)
            color_display.setStyleSheet(f"background-color: {self.theme.get(key, '#ffffff')}")
            hbox.addWidget(label)
            hbox.addWidget(color_display)
            hbox.addWidget(btn)
            layout.addLayout(hbox)
            self.color_buttons[key] = color_display

        apply_btn = QPushButton("Apply")
        apply_btn.clicked.connect(self.accept)
        layout.addWidget(apply_btn)

        self.setLayout(layout)

    def pick_color(self, key):
        current = self.theme.get(key, "#ffffff")
        color = QColorDialog.getColor(QColor(current), self, f"Choose {self.color_fields[key]}")
        if color.isValid():
            hex_color = color.name()
            self.theme[key] = hex_color
            self.color_buttons[key].setStyleSheet(f"background-color: {hex_color}")

    def get_theme(self):
        return self.theme


class ProjectLoadingDialog(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent, Qt.Window | Qt.WindowTitleHint | Qt.CustomizeWindowHint)
        self.setWindowTitle("Opening Project")
        self.setFixedSize(300, 100)
        self.setWindowModality(Qt.ApplicationModal)

        layout = QVBoxLayout()
        label = QLabel("Cyber Ninjas Building New Project...\nPlease Hold...")
        label.setAlignment(Qt.AlignCenter)
        layout.addWidget(label)
        self.setLayout(layout)


# -----------------------------
# Main App
# -----------------------------
class StopMotionApp(QWidget):
    RES_CHOICES = [
        "Auto",
        "640x480",
        "1280x720",
        "1920x1080",
        "2560x1440",
        "3840x2160",
    ]

    AUTO_TRY = [
        (3840, 2160),
        (2560, 1440),
        (1920, 1080),
        (1280, 720),
        (640, 480),
    ]

    def __init__(self):
        super().__init__()
        self.setWindowTitle("CN Stop Motion App by Sensei Jesse")

        # Services
        self.camera = CameraService()
        self.project = ProjectStore()
        self.undo = UndoManager()

        # Camera discovery/open state
        self.available_cameras: Dict[int, str] = {}
        self.current_camera_index: Optional[int] = 0
        self.current_camera_name: Optional[str] = None
        self.camera_search_thread: Optional[CameraSearchThread] = None
        self.camera_open_thread: Optional[CameraOpenThread] = None

        # Playback state
        self.is_playback_mode = False
        self.loop_playback = True
        self.gif_loop_value = 0 if self.loop_playback else 1
        self.playback_index = 0

        # Last frame
        self.latest_frame: Optional[np.ndarray] = None

        # UI widgets
        self.camera_selector = QComboBox()
        self.capture_btn = QPushButton("Capture Frame")
        self.capture_btn.setEnabled(False)

        self.video_label = QLabel()
        self.video_label.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.video_label.setMinimumSize(640, 480)

        self.timeline = QListWidget()
        self.timeline.setFixedHeight(100)
        self.timeline.itemClicked.connect(self.preview_selected_frame)
        self.timeline.setViewMode(QListWidget.IconMode)
        self.timeline.setMovement(QListWidget.Static)
        self.timeline.setSpacing(5)
        self.timeline.setIconSize(QSize(100, 80))
        self.timeline.setFlow(QListWidget.LeftToRight)
        self.timeline.setResizeMode(QListWidget.Adjust)
        self.timeline.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOn)
        self.timeline.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.timeline.setWrapping(False)

        # Enable multi-select (needed for Dup Reverse)
        self.timeline.setSelectionMode(QListWidget.ExtendedSelection)

        self.delete_btn = QPushButton("Delete Frame")
        self.duplicate_btn = QPushButton("Duplicate Frame")

        # NEW BUTTON: Duplicate selected in reverse
        self.dup_reverse_btn = QPushButton("Dup Reverse")
        self.dup_reverse_btn.setToolTip("Duplicate selected frame(s) in reverse order")
        self.dup_reverse_btn.clicked.connect(self.duplicate_selected_reverse)

        self.undo_btn = QPushButton("Undo")
        self.redo_btn = QPushButton("Redo")
        self.save_btn = QPushButton("Save Project")
        self.open_btn = QPushButton("Open Project")
        self.new_project_btn = QPushButton("New Project")

        self.play_pause_btn = QPushButton("Play")
        self.play_pause_btn.setCheckable(True)

        self.back_to_live_btn = QPushButton("Back to Live Feed")

        self.loop_checkbox = QCheckBox("Loop")
        self.loop_checkbox.setChecked(True)

        self.export_btn = QPushButton("Export MP4")
        self.export_gif_btn = QPushButton("Export GIF")

        self.opacity_slider = QSlider(Qt.Horizontal)
        self.opacity_slider.setRange(0, 100)
        self.opacity_slider.setValue(50)

        self.fps_spin = QSpinBox()
        self.fps_spin.setRange(1, 60)
        self.fps_spin.setValue(12)

        self.onion_layer_spin = QSpinBox()
        self.onion_layer_spin.setRange(1, 10)
        self.onion_layer_spin.setValue(3)
        self.onion_layer_spin.setToolTip("Number of onion skin layers to display")

        self.onion_checkbox = QCheckBox("Onion Skin")
        self.onion_checkbox.setChecked(True)

        # Resolution UI
        self.resolution_selector = QComboBox()
        self.resolution_selector.addItems(self.RES_CHOICES)
        self.resolution_selector.setCurrentText("1280x720")
        self.resolution_selector.setToolTip("Requested camera resolution (camera may clamp to nearest supported)")
        self.resolution_status = QLabel("")

        # Theme UI
        self.theme_label = QLabel("Theme:")
        self.theme_selector = QComboBox()
        self.theme_selector.addItems(["System Default", "Dark", "Custom", "Light"])
        self.theme_selector.setCurrentText("System Default")
        self.edit_theme_btn = QPushButton("Edit Custom Theme")
        self.custom_theme: Dict[str, str] = {}

        # Dialog refs
        self.project_loading_dialog: Optional[ProjectLoadingDialog] = None
        self.camera_search_dialog: Optional[CameraSearchDialog] = None
        self.camera_loading_dialog: Optional[QDialog] = None

        # Wiring
        self.capture_btn.clicked.connect(self.capture_frame)
        self.delete_btn.clicked.connect(self.delete_frame)
        self.duplicate_btn.clicked.connect(self.duplicate_frame)
        self.undo_btn.clicked.connect(self.undo_action)
        self.redo_btn.clicked.connect(self.redo_action)
        self.save_btn.clicked.connect(self.save_project)
        self.open_btn.clicked.connect(self.open_project)
        self.new_project_btn.clicked.connect(self.new_project)
        self.play_pause_btn.toggled.connect(self.play_pause_toggle)
        self.back_to_live_btn.clicked.connect(self.resume_live_feed)
        self.loop_checkbox.stateChanged.connect(self.toggle_loop)
        self.export_btn.clicked.connect(self.export_mp4)
        self.export_gif_btn.clicked.connect(self.export_gif)
        self.opacity_slider.valueChanged.connect(self.update_onion_skin)
        self.camera_selector.currentIndexChanged.connect(self.change_camera)

        self.theme_selector.currentTextChanged.connect(self.change_theme)
        self.edit_theme_btn.clicked.connect(self.open_theme_editor)

        # Resolution change forces reopen
        self.resolution_selector.currentTextChanged.connect(self.on_resolution_changed)

        # Tooltips
        self.capture_btn.setToolTip("Take a snapshot from the live feed")
        self.delete_btn.setToolTip("Remove selected frame")
        self.duplicate_btn.setToolTip("Make a copy of the selected frame")
        self.undo_btn.setToolTip("Undo last action")
        self.redo_btn.setToolTip("Redo last undone action")
        self.save_btn.setToolTip("Save current project")
        self.open_btn.setToolTip("Load existing project")
        self.new_project_btn.setToolTip("Start a new project")
        self.play_pause_btn.setToolTip("Play/Pause preview")

        # Layout (unchanged except resolution controls + new dup reverse button)
        layout = QVBoxLayout()

        camera_layout = QHBoxLayout()
        camera_layout.addWidget(QLabel("Select Camera:"))
        camera_layout.addWidget(self.camera_selector)

        self.rescan_btn = QPushButton("Rescan")
        self.rescan_btn.setToolTip("Rescan for available cameras")
        self.rescan_btn.clicked.connect(self.start_camera_search)
        camera_layout.addWidget(self.rescan_btn)

        camera_layout.addWidget(QLabel("Resolution:"))
        camera_layout.addWidget(self.resolution_selector)
        camera_layout.addWidget(self.resolution_status)

        camera_layout.addWidget(self.theme_label)
        camera_layout.addWidget(self.theme_selector)
        camera_layout.addWidget(self.edit_theme_btn)

        layout.addLayout(camera_layout)
        layout.addWidget(self.video_label)

        controls = QHBoxLayout()
        controls.addWidget(self.capture_btn)
        controls.addWidget(self.delete_btn)
        controls.addWidget(self.duplicate_btn)
        controls.addWidget(self.dup_reverse_btn)  # <-- NEW BUTTON next to Duplicate
        controls.addWidget(self.undo_btn)
        controls.addWidget(self.redo_btn)
        controls.addWidget(self.new_project_btn)
        controls.addWidget(self.save_btn)
        controls.addWidget(self.open_btn)
        controls.addWidget(self.play_pause_btn)
        controls.addWidget(self.loop_checkbox)

        fps_layout = QHBoxLayout()
        fps_layout.addWidget(QLabel("FPS:"))
        fps_layout.addWidget(self.fps_spin)
        fps_container = QWidget()
        fps_container.setLayout(fps_layout)
        controls.addWidget(fps_container)

        controls.addWidget(self.export_btn)
        controls.addWidget(self.export_gif_btn)
        controls.addWidget(self.back_to_live_btn)

        layout.addLayout(controls)

        layout.addWidget(QLabel("Timeline:"))
        layout.addWidget(self.timeline)

        onion_layout = QHBoxLayout()
        onion_layout.addWidget(QLabel("Onion Skin Opacity:"))
        onion_layout.addWidget(self.opacity_slider)
        onion_layout.addWidget(QLabel("Onion Layers:"))
        onion_layout.addWidget(self.onion_layer_spin)
        onion_layout.addWidget(self.onion_checkbox)
        layout.addLayout(onion_layout)

        self.setLayout(layout)

        # Timers
        self.timer = QTimer()
        self.timer.timeout.connect(self.update_frame)

        self.autosave_timer = QTimer()
        self.autosave_timer.timeout.connect(self.save_project)
        self.autosave_timer.start(300_000)  # 5 minutes

        self.playback_timer = QTimer()
        self.playback_timer.timeout.connect(self.playback_next_frame)

        # Shortcuts
        QShortcut(QKeySequence("Ctrl+Z"), self).activated.connect(self.undo_action)
        QShortcut(QKeySequence("Ctrl+Shift+Z"), self).activated.connect(self.redo_action)

        # Start camera search after UI shows
        QTimer.singleShot(500, self.start_camera_search)

    # -----------------------------
    # Resolution helpers
    # -----------------------------
    def _parse_resolution(self, text: str) -> Optional[Tuple[int, int]]:
        if text.strip().lower() == "auto":
            return None
        try:
            w_str, h_str = text.lower().split("x", 1)
            return int(w_str), int(h_str)
        except Exception:
            return None

    def _read_back_resolution(self, cap: cv2.VideoCapture) -> Tuple[int, int]:
        w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
        h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
        return w, h

    def _set_resolution(self, cap: cv2.VideoCapture, w: int, h: int) -> Tuple[int, int]:
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, w)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, h)
        return self._read_back_resolution(cap)

    def apply_resolution_to_cap(self, cap: cv2.VideoCapture) -> Tuple[int, int]:
        # Try MJPG to unlock higher resolutions on many webcams (especially Windows/DirectShow)
        try:
            cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
        except Exception:
            pass

        choice = self.resolution_selector.currentText().strip()
        desired = self._parse_resolution(choice)

        if desired is None:
            for (w, h) in self.AUTO_TRY:
                actual_w, actual_h = self._set_resolution(cap, w, h)
                if actual_w >= int(w * 0.9) and actual_h >= int(h * 0.9):
                    return actual_w, actual_h
            return self._read_back_resolution(cap)

        w, h = desired
        return self._set_resolution(cap, w, h)

    def update_resolution_status_label(self, w: int, h: int) -> None:
        if w > 0 and h > 0:
            self.resolution_status.setText(f"(applied {w}x{h})")
        else:
            self.resolution_status.setText("")

    def on_resolution_changed(self, _text: str) -> None:
        if self.current_camera_index is None:
            return
        if self.camera_open_thread and self.camera_open_thread.isRunning():
            return
        self.open_camera(self.current_camera_index, force=True)

    # -----------------------------
    # Camera search/open
    # -----------------------------
    def start_camera_search(self):
        if self.camera_search_thread and self.camera_search_thread.isRunning():
            return

        self.camera_search_dialog = CameraSearchDialog(self)
        self.camera_search_dialog.show()

        self.camera_search_thread = CameraSearchThread()
        self.camera_search_thread.cameras_found.connect(self.on_cameras_found)
        self.camera_search_thread.finished.connect(self.cleanup_camera_search_thread)
        self.camera_search_thread.start()

    def cleanup_camera_search_thread(self):
        if self.camera_search_thread:
            self.camera_search_thread.deleteLater()
            self.camera_search_thread = None

    def on_cameras_found(self, cameras: List[Tuple[int, str]]):
        if self.camera_search_dialog:
            self.camera_search_dialog.close()
            self.camera_search_dialog = None

        self.available_cameras = {index: name for index, name in cameras}

        self.camera_selector.blockSignals(True)
        self.camera_selector.clear()
        for index, name in cameras:
            self.camera_selector.addItem(name, index)
        self.camera_selector.blockSignals(False)

        if not cameras:
            self.camera_selector.addItem("No Camera Found")
            self.capture_btn.setEnabled(False)
            QMessageBox.warning(self, "No Cameras", "No cameras were found! Did you hide them too well?")
            self.current_camera_index = None
            self.current_camera_name = None
            self.camera.release()
            self.update_resolution_status_label(0, 0)
            return

        matching_index: Optional[int] = None
        if self.current_camera_name:
            for idx, name in self.available_cameras.items():
                if name == self.current_camera_name:
                    matching_index = idx
                    break

        if matching_index is None:
            matching_index = cameras[0][0]
            self.current_camera_name = cameras[0][1]

        combo_index = self.camera_selector.findData(matching_index)
        if combo_index != -1:
            self.camera_selector.setCurrentIndex(combo_index)

        self.current_camera_index = matching_index
        self.open_camera(self.current_camera_index)

    def open_camera(self, index: int, force: bool = False):
        camera_name = self.available_cameras.get(index)

        # Only short-circuit if NOT forcing
        if (not force) and self.camera.is_open() and self.current_camera_index == index and self.current_camera_name == camera_name:
            log.info("Camera already open and matches requested index and name.")
            return

        if self.camera_open_thread and self.camera_open_thread.isRunning():
            log.info("Camera open thread still running, ignoring open request")
            return

        if force:
            self.camera.release()

        self.current_camera_index = index
        self.camera_selector.setEnabled(False)
        self.capture_btn.setEnabled(False)
        self.video_label.setText("Loading camera feed... Cyber Ninja's working their hardest")
        self.video_label.setAlignment(Qt.AlignCenter)

        self.camera_loading_dialog = QDialog(self)
        self.camera_loading_dialog.setWindowTitle("Switching Camera")
        dlg_layout = QVBoxLayout()
        label = QLabel("Please wait... Cyber Ninjas are changing cameras.")
        dlg_layout.addWidget(label)
        self.camera_loading_dialog.setLayout(dlg_layout)
        self.camera_loading_dialog.setModal(True)
        self.camera_loading_dialog.setFixedSize(300, 100)
        self.camera_loading_dialog.show()

        self.camera_open_thread = CameraOpenThread(index)
        self.camera_open_thread.camera_opened.connect(self.on_camera_opened)
        self.camera_open_thread.finished.connect(self.cleanup_camera_thread)
        self.camera_open_thread.start()

    def cleanup_camera_thread(self):
        if self.camera_open_thread:
            if self.camera_open_thread.isRunning():
                self.camera_open_thread.quit()
                self.camera_open_thread.wait()
            self.camera_open_thread.deleteLater()
            self.camera_open_thread = None

    def on_camera_opened(self, success: bool, index: int, cap: Optional[cv2.VideoCapture]):
        self.camera_selector.setEnabled(True)

        if self.camera_loading_dialog:
            self.camera_loading_dialog.close()
            self.camera_loading_dialog = None

        if success and cap is not None:
            try:
                actual_w, actual_h = self.apply_resolution_to_cap(cap)
            except Exception as e:
                log.warning("Failed to apply resolution: %s", e)
                actual_w, actual_h = self._read_back_resolution(cap)

            self.update_resolution_status_label(actual_w, actual_h)
            log.info("Resolution requested=%s applied=%dx%d", self.resolution_selector.currentText(), actual_w, actual_h)

            self.camera.set_cap(cap)

            self.current_camera_index = index
            self.current_camera_name = self.available_cameras.get(index, None)
            self.capture_btn.setEnabled(True)

            if self.playback_timer.isActive():
                self.playback_timer.stop()
                self.play_pause_btn.setChecked(False)

            if not self.timer.isActive():
                self.timer.start(30)
        else:
            if index == 0:
                self.start_camera_search()
            else:
                QMessageBox.warning(self, "Camera Error", f"Cyber Ninjas can't open camera {index}")
            self.capture_btn.setEnabled(False)
            self.update_resolution_status_label(0, 0)

    def change_camera(self, index: int):
        if index < 0:
            return
        selected_index = self.camera_selector.itemData(index)
        if selected_index is None:
            return
        if selected_index == self.current_camera_index:
            return
        self.current_camera_index = selected_index
        self.open_camera(self.current_camera_index)

    # -----------------------------
    # Live view / playback
    # -----------------------------
    def update_frame(self):
        if self.is_playback_mode:
            return

        try:
            ok, frame = self.camera.read()
            if not ok or frame is None:
                log.info("Frame read failed. Releasing and retrying...")
                self.camera.release()
                self.latest_frame = None
                QTimer.singleShot(1000, self.safe_resume_camera)
                return

            self.latest_frame = frame.copy()

            if self.onion_checkbox.isChecked() and self.project.frames:
                self.update_onion_skin()
            else:
                rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                h, w, ch = rgb.shape
                bytes_per_line = ch * w
                qt_image = QImage(rgb.data, w, h, bytes_per_line, QImage.Format_RGB888)

                pix = QPixmap.fromImage(qt_image).scaled(
                    self.video_label.width(),
                    self.video_label.height(),
                    Qt.KeepAspectRatio,
                    Qt.SmoothTransformation,
                )
                self.video_label.setPixmap(pix)
                if self.video_label.text():
                    self.video_label.setText("")
        except Exception as e:
            log.exception("Exception in update_frame: %s", e)
            self.camera.release()
            self.latest_frame = None

    def safe_resume_camera(self):
        if self.camera_open_thread and self.camera_open_thread.isRunning():
            log.info("Camera open thread is still running. Delaying resume.")
            QTimer.singleShot(1000, self.safe_resume_camera)
            return
        self.resume_live_feed()

    def resume_live_feed(self):
        log.info("resume_live_feed called")

        if self.playback_timer.isActive():
            self.playback_timer.stop()
            self.play_pause_btn.setChecked(False)

        if self.camera.is_open():
            log.info("Camera already opened. Restarting timer if needed.")
            if not self.timer.isActive():
                self.timer.start(30)
            return

        log.info("Camera not available. Releasing and rescanning.")
        self.camera.release()
        self.start_camera_search()
        QTimer.singleShot(1500, self.try_other_camera_if_still_dead)

    def preview_selected_frame(self, item: QListWidgetItem):
        self.timer.stop()
        frame_path = item.data(Qt.UserRole)
        frame = cv2.imread(frame_path)

        if isinstance(frame, np.ndarray):
            height, width, _ = frame.shape
            bytes_per_line = 3 * width
            q_img = QImage(frame.data, width, height, bytes_per_line, QImage.Format_RGB888).rgbSwapped()
            pix = QPixmap.fromImage(q_img).scaled(
                self.video_label.width(),
                self.video_label.height(),
                Qt.KeepAspectRatio,
                Qt.SmoothTransformation,
            )
            self.video_label.setPixmap(pix)
        else:
            log.warning("Warning: Expected image data but got something else")

    def toggle_loop(self, state: int):
        self.loop_playback = bool(state)
        self.gif_loop_value = 0 if self.loop_playback else 1

    def play_pause_toggle(self, checked: bool):
        if checked:
            self.play_pause_btn.setText("Pause")
            self.is_playback_mode = True
            self.playback_index = 0
            self.playback_timer.start(int(1000 / self.fps_spin.value()))
        else:
            self.play_pause_btn.setText("Play")
            self.is_playback_mode = False
            self.playback_timer.stop()

    def playback_next_frame(self):
        if not self.project.frames:
            self.play_pause_btn.setChecked(False)
            self.playback_timer.stop()
            return

        if self.playback_index >= len(self.project.frames):
            if self.loop_playback:
                self.playback_index = 0
            else:
                self.play_pause_btn.setChecked(False)
                self.playback_timer.stop()
                return

        frame_path = self.project.frames[self.playback_index]
        if not os.path.exists(frame_path):
            log.info("Frame path does not exist: %s", frame_path)
            self.playback_index += 1
            return

        pixmap = QPixmap(frame_path).scaled(
            self.video_label.width(),
            self.video_label.height(),
            Qt.KeepAspectRatio,
            Qt.SmoothTransformation,
        )
        self.video_label.setPixmap(pixmap)
        self.playback_index += 1

    # -----------------------------
    # Frames + timeline
    # -----------------------------
    def refresh_timeline(self):
        self.timeline.clear()
        icon_size = 80
        valid_frames: List[str] = []

        for frame_path in self.project.frames:
            if not os.path.exists(frame_path):
                log.info("Missing file: %s", frame_path)
                continue

            frame = cv2.imread(frame_path)
            if frame is None:
                log.info("Unreadable image file: %s", frame_path)
                continue

            try:
                frame_rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            except cv2.error as e:
                log.info("OpenCV error on frame %s: %s", frame_path, e)
                continue

            h, w, ch = frame_rgb.shape
            bytes_per_line = ch * w
            q_img = QImage(frame_rgb.data, w, h, bytes_per_line, QImage.Format_RGB888)
            if q_img.isNull():
                log.info("Null QImage from frame: %s", frame_path)
                continue

            thumb = QPixmap.fromImage(q_img).scaledToHeight(icon_size, Qt.SmoothTransformation)
            item = QListWidgetItem(QIcon(thumb), f"{len(valid_frames)}")
            item.setData(Qt.UserRole, frame_path)
            item.setSizeHint(QSize(icon_size + 10, icon_size + 20))
            self.timeline.addItem(item)

            valid_frames.append(frame_path)

        self.project.frames = valid_frames

    def capture_frame(self):
        if self.latest_frame is None:
            QMessageBox.warning(self, "Capture Failed", "No frame available to capture.")
            return

        if not self.project.has_project():
            QMessageBox.warning(self, "No Project", "Please create a new project before capturing frames.")
            return

        try:
            index, path = self.project.capture_from_bgr(self.latest_frame)
            self.undo.push(Action(type="add", path=path, index=index))
        except Exception as e:
            QMessageBox.critical(self, "Capture Failed", str(e))
            return

        self.refresh_timeline()
        self.timeline.scrollToBottom()

    def delete_frame(self):
        selected_items = self.timeline.selectedItems()
        if not selected_items:
            return

        reply = QMessageBox.question(
            self, "Delete Frame(s)",
            f"Are you sure you want to delete {len(selected_items)} frame(s)?",
            QMessageBox.Yes | QMessageBox.No
        )
        if reply == QMessageBox.No:
            return

        rows = sorted((self.timeline.row(item) for item in selected_items), reverse=True)

        for row in rows:
            try:
                action = self.project.delete_frame(row)
                self.undo.push(action)
            except Exception as e:
                log.warning("Delete failed at row %s: %s", row, e)

        self.refresh_timeline()
        self.resume_live_feed()

    def duplicate_frame(self):
        selected_items = self.timeline.selectedItems()
        if not selected_items:
            QMessageBox.information(self, "No Frame Selected", "Please select a frame to duplicate.")
            return

        for item in selected_items:
            index = self.timeline.row(item)
            try:
                insert_at, new_path = self.project.duplicate_frame(index)
                self.undo.push(Action(type="add", path=new_path, index=insert_at))
            except FileNotFoundError as e:
                QMessageBox.warning(self, "Error", f"Original frame is missing:\n{e}")
            except Exception as e:
                QMessageBox.critical(self, "Duplicate Failed", f"Could not copy frame:\n{e}")

        self.refresh_timeline()
        self.timeline.scrollToBottom()

    # -------- NEW FEATURE: duplicate selected frames in reverse order --------
    def _make_unique_frame_path(self) -> str:
        """
        Generates a unique frame_XXXX.png path in the project folder.
        This avoids collisions when inserting duplicates mid-list.
        """
        base_dir = self.project.project_path
        if not base_dir:
            raise RuntimeError("No project open")

        i = max(0, len(self.project.frames))
        while True:
            name = f"frame_{i:04d}.png"
            path = os.path.join(base_dir, name)
            if (not os.path.exists(path)) and (path not in self.project.frames):
                return path
            i += 1

    def duplicate_selected_reverse(self) -> None:
        selected_items = self.timeline.selectedItems()
        if not selected_items:
            QMessageBox.information(self, "No Frames Selected", "Please select one or more frames.")
            return

        if not self.project.has_project():
            QMessageBox.warning(self, "No Project", "Please create or open a project first.")
            return

        # Selected rows in ascending order
        selected_rows = sorted(self.timeline.row(item) for item in selected_items)
        if not selected_rows:
            return

        # Source paths in reverse order
        source_paths = [self.project.frames[r] for r in reversed(selected_rows)]

        # Insert after the last selected frame
        insert_at = selected_rows[-1] + 1

        added_any = False

        for offset, src_path in enumerate(source_paths):
            if not os.path.exists(src_path):
                QMessageBox.warning(self, "Missing Frame", f"Frame is missing:\n{src_path}")
                continue

            dst_path = self._make_unique_frame_path()

            try:
                shutil.copy(src_path, dst_path)
            except Exception as e:
                QMessageBox.critical(self, "Duplicate Failed", f"Could not copy:\n{src_path}\n\n{e}")
                continue

            idx = insert_at + offset
            self.project.frames.insert(idx, dst_path)

            # Push undo action for each inserted duplicate.
            # (Redo stays correct; this just means "batch" undo is one-per-copy like your other operations.)
            self.undo.push(Action(type="add", path=dst_path, index=idx))

            added_any = True

        if added_any:
            self.project.unsaved_changes = True
            self.refresh_timeline()
            self.timeline.scrollToBottom()
    # ----------------------------------------------------------------------

    def undo_action(self):
        action = self.undo.pop_undo()
        if not action:
            return

        if action.type == "add":
            self.project.remove_added(action)
        elif action.type == "delete":
            self.project.restore_deleted(action)

        self.refresh_timeline()

    def redo_action(self):
        action = self.undo.pop_redo()
        if not action:
            return

        if action.type == "add":
            self.project.reapply_add(action)
        elif action.type == "delete":
            self.project.reapply_delete(action)

        self.refresh_timeline()

    # -----------------------------
    # Onion skin (uses latest_frame)
    # -----------------------------
    def update_onion_skin(self):
        if self.latest_frame is None:
            return
        if not self.project.frames:
            return

        live_frame = self.latest_frame
        live_rgba = cv2.cvtColor(live_frame, cv2.COLOR_BGR2RGBA)
        height, width = live_rgba.shape[:2]
        composite = live_rgba.astype(float)

        max_layers = self.onion_layer_spin.value()
        num_frames = len(self.project.frames)
        layers_to_show = min(max_layers, num_frames)
        base_opacity = self.opacity_slider.value() / 100.0

        for i in range(1, layers_to_show + 1):
            frame_path = self.project.frames[-i]
            previous = cv2.imread(frame_path, cv2.IMREAD_UNCHANGED)
            if previous is None:
                continue

            previous = cv2.resize(previous, (width, height))
            if previous.shape[-1] == 4:
                previous_rgba = previous
            else:
                previous_rgba = cv2.cvtColor(previous, cv2.COLOR_BGR2RGBA)

            layer_opacity = base_opacity / i
            composite = cv2.addWeighted(previous_rgba.astype(float), layer_opacity, composite, 1.0, 0)

        composite = np.clip(composite, 0, 255).astype(np.uint8)
        qt_image = QImage(composite.data, width, height, QImage.Format_RGBA8888)

        pix = QPixmap.fromImage(qt_image).scaled(
            self.video_label.width(),
            self.video_label.height(),
            Qt.KeepAspectRatio,
            Qt.SmoothTransformation,
        )
        self.video_label.setPixmap(pix)

    # -----------------------------
    # Project create/open/save
    # -----------------------------
    def new_project(self):
        if self.project.unsaved_changes:
            reply = QMessageBox.question(
                self, "Unsaved Changes",
                "You have unsaved changes. Do you want to continue and lose them?",
                QMessageBox.Yes | QMessageBox.No
            )
            if reply == QMessageBox.No:
                return

        self.timer.stop()
        self.autosave_timer.stop()

        options = QFileDialog.Options()
        options |= QFileDialog.DontUseNativeDialog
        folder = QFileDialog.getExistingDirectory(self, "Create New Project Folder", options=options)

        self.timer.start(30)
        self.autosave_timer.start(300_000)

        if folder:
            self.project_loading_dialog = ProjectLoadingDialog(self)
            self.project_loading_dialog.show()

            self.project.reset(folder)
            self.undo.clear()

            self.refresh_timeline()
            self.project.unsaved_changes = False

            if self.current_camera_index is not None:
                self.open_camera(self.current_camera_index)

            if self.project_loading_dialog:
                self.project_loading_dialog.close()
                self.project_loading_dialog = None

    def save_project(self):
        if self.project.project_path:
            undo_folder = os.path.join(self.project.project_path, ".undo_cache")
            if os.path.exists(undo_folder):
                shutil.rmtree(undo_folder, ignore_errors=True)

            self.save_metadata()
            QMessageBox.information(self, "Project Saved", f"Project saved in: {self.project.project_path}")
            self.project.unsaved_changes = False

    def open_project(self):
        if self.project.unsaved_changes:
            reply = QMessageBox.question(
                self, "Unsaved Changes",
                "You have unsaved changes. Do you want to save them before opening a new project?",
                QMessageBox.Yes | QMessageBox.No | QMessageBox.Cancel
            )
            if reply == QMessageBox.Cancel:
                return
            elif reply == QMessageBox.Yes:
                self.save_project()

        options = QFileDialog.Options()
        options |= QFileDialog.DontUseNativeDialog
        folder = QFileDialog.getExistingDirectory(self, "Open Project Folder", options=options)

        if folder:
            self.project_loading_dialog = ProjectLoadingDialog(self)
            self.project_loading_dialog.show()

            self.project.reset(folder)
            self.undo.clear()
            self.project.unsaved_changes = False

            self.project.load_frames_from_disk()
            self.refresh_timeline()
            self.load_metadata()

            if self.current_camera_index is not None:
                self.open_camera(self.current_camera_index)

            if self.project_loading_dialog:
                self.project_loading_dialog.close()
                self.project_loading_dialog.deleteLater()
                self.project_loading_dialog = None

    # -----------------------------
    # Export
    # -----------------------------
    def export_mp4(self):
        if not self.project.frames:
            QMessageBox.warning(self, "Export Error", "No frames to export!")
            return

        save_path, _ = QFileDialog.getSaveFileName(self, "Save MP4 Video", "", "MP4 files (*.mp4)")
        if not save_path:
            return

        if not save_path.lower().endswith(".mp4"):
            save_path += ".mp4"

        fps = self.fps_spin.value()

        first_frame = cv2.imread(self.project.frames[0])
        if first_frame is None:
            QMessageBox.warning(self, "Export Error", "Failed to read first frame!")
            return

        height, width, _ = first_frame.shape
        fourcc = cv2.VideoWriter_fourcc(*"mp4v")
        video_writer = cv2.VideoWriter(save_path, fourcc, fps, (width, height))

        if not video_writer.isOpened():
            QMessageBox.critical(self, "Export Error", "Failed to open video writer!")
            return

        for frame_path in self.project.frames:
            frame = cv2.imread(frame_path)
            if frame is None:
                continue
            if frame.shape[1] != width or frame.shape[0] != height:
                frame = cv2.resize(frame, (width, height))
            video_writer.write(frame)

        video_writer.release()
        QMessageBox.information(self, "Export Complete", f"MP4 video saved to:\n{save_path}")

    def export_gif(self):
        if not self.project.frames:
            QMessageBox.warning(self, "Export Error", "No frames to export!")
            return

        import imageio.v2 as imageio

        save_path, _ = QFileDialog.getSaveFileName(self, "Save GIF Animation", "", "GIF files (*.gif)")
        if not save_path:
            return

        fps = self.fps_spin.value()
        duration = 1 / fps

        images = []
        bad_frames = []
        for frame_path in self.project.frames:
            try:
                img = imageio.imread(frame_path)
                images.append(img)
            except Exception as e:
                bad_frames.append(frame_path)
                log.warning("Warning: Could not load frame %s: %s", frame_path, e)

        if not images:
            QMessageBox.warning(self, "Export Error", "No valid frames to export.")
            return

        try:
            imageio.mimsave(save_path, images, duration=duration, loop=self.gif_loop_value)
        except Exception as e:
            QMessageBox.critical(self, "Export Failed", f"Could not save GIF:\n{e}")
            return

        if bad_frames:
            QMessageBox.warning(
                self,
                "Partial Export",
                "Some frames could not be loaded and were skipped:\n\n" + "\n".join(bad_frames)
            )
        else:
            QMessageBox.information(self, "Export Complete", f"GIF animation saved to:\n{save_path}")

    # -----------------------------
    # Metadata + theme
    # -----------------------------
    def save_metadata(self):
        metadata = {
            "fps": self.fps_spin.value(),
            "onion_opacity": self.opacity_slider.value(),
            "onion_layers": self.onion_layer_spin.value(),
            "loop_playback": self.loop_checkbox.isChecked(),
            "theme": self.theme_selector.currentText(),
            "custom_theme": getattr(self, "custom_theme", None),
            "resolution": self.resolution_selector.currentText(),
        }
        self.project.save_metadata(metadata)

    def load_metadata(self):
        metadata = self.project.load_metadata()
        if not metadata:
            return

        self.fps_spin.setValue(metadata.get("fps", 12))
        self.opacity_slider.setValue(metadata.get("onion_opacity", 50))
        self.onion_layer_spin.setValue(metadata.get("onion_layers", 3))
        self.loop_checkbox.setChecked(metadata.get("loop_playback", True))

        theme = metadata.get("theme", "System Default")
        self.custom_theme = metadata.get("custom_theme") or {}
        if not isinstance(self.custom_theme, dict):
            self.custom_theme = {}

        if theme in ["Light", "Dark", "Custom", "System Default"]:
            self.theme_selector.setCurrentText(theme)
        else:
            self.theme_selector.setCurrentText("Light")

        if theme == "Custom":
            self.change_theme("Custom")

        res = metadata.get("resolution")
        if isinstance(res, str) and res in self.RES_CHOICES:
            self.resolution_selector.blockSignals(True)
            self.resolution_selector.setCurrentText(res)
            self.resolution_selector.blockSignals(False)

    def change_theme(self, theme_name: str):
        if theme_name == "System Default":
            self.setStyleSheet("")
        elif theme_name == "Dark":
            dark_stylesheet = """
                QWidget {
                    background-color: #2b2b2b;
                    color: #f0f0f0;
                }
                QPushButton {
                    background-color: #444;
                    color: white;
                    border: 1px solid #666;
                    padding: 4px;
                }
            """
            self.setStyleSheet(dark_stylesheet)
        elif theme_name == "Custom":
            defaults = {
                "bg_color": "#2b2b2b",
                "text_color": "#f0f0f0",
                "button_bg": "#444",
                "button_text": "white"
            }
            if not isinstance(getattr(self, "custom_theme", None), dict):
                self.custom_theme = {}
            theme = {**defaults, **self.custom_theme}
            css = f"""
                QWidget {{
                    background-color: {theme['bg_color']};
                    color: {theme['text_color']};
                }}
                QPushButton {{
                    background-color: {theme['button_bg']};
                    color: {theme['button_text']};
                    border: 1px solid #666;
                    padding: 4px;
                }}
            """
            self.setStyleSheet(css)
        elif theme_name == "Light":
            light_stylesheet = """
                QWidget {
                    background-color: #f0f0f0;
                    color: #2b2b2b;
                }
                QPushButton {
                    background-color: #ddd;
                    color: #000;
                    border: 1px solid #aaa;
                    padding: 4px;
                }
                QLabel, QCheckBox {
                    color: #2b2b2b;
                }
                QComboBox {
                    background-color: #eee;
                    color: #000;
                    border: 1px solid #aaa;
                }
                QListWidget {
                    background-color: #ffffff;
                    color: #000000;
                }
            """
            self.setStyleSheet(light_stylesheet)

    def open_theme_editor(self):
        if not hasattr(self, "custom_theme") or not isinstance(self.custom_theme, dict):
            self.custom_theme = {}

        dlg = ThemeEditorDialog(self, self.custom_theme)
        if dlg.exec():
            self.custom_theme = dlg.get_theme()
            self.change_theme("Custom")
            self.save_metadata()

    # -----------------------------
    # Camera fallback
    # -----------------------------
    def try_other_camera_if_still_dead(self):
        if not self.available_cameras:
            log.info("No cameras found after rescan.")
            QMessageBox.warning(self, "No Cameras", "No cameras were found after rescan.")
            return

        if self.camera.is_open():
            log.info("Fallback not needed; camera resumed.")
            return

        log.info("Trying other available cameras as fallback...")
        for idx in self.available_cameras:
            if idx != self.current_camera_index:
                log.info("Fallback to camera index %s", idx)
                self.current_camera_index = idx
                self.open_camera(idx)

                combo_index = self.camera_selector.findData(idx)
                if combo_index != -1:
                    self.camera_selector.setCurrentIndex(combo_index)
                return

        log.info("No alternate working cameras available.")

    # -----------------------------
    # Close
    # -----------------------------
    def closeEvent(self, event):
        if self.project.unsaved_changes:
            reply = QMessageBox.question(
                self, "Unsaved Changes",
                "You have unsaved changes. Are you sure you want to quit?",
                QMessageBox.Yes | QMessageBox.No
            )
            if reply == QMessageBox.No:
                event.ignore()
                return

        log.info("Closing app...")

        # Purge trash on close (requested behavior)
        try:
            if self.project.has_project():
                self.project.purge_trash()
        except Exception as e:
            log.warning("Failed to purge trash: %s", e)

        if self.camera_open_thread and self.camera_open_thread.isRunning():
            log.info("Waiting for camera thread to finish...")
            self.camera_open_thread.quit()
            self.camera_open_thread.wait()

        if self.camera_open_thread:
            self.camera_open_thread.deleteLater()
            self.camera_open_thread = None

        self.timer.stop()
        self.playback_timer.stop()
        self.camera.release()

        log.info("Closed cleanly.")
        event.accept()


if __name__ == "__main__":
    try:
        app = QApplication(sys.argv)
        window = StopMotionApp()
        window.show()
        sys.exit(app.exec())
    except Exception:
        with open("crashlog.txt", "w", encoding="utf-8") as f:
            traceback.print_exc(file=f)
        raise
