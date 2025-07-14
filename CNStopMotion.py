import sys
import os
import cv2
import shutil
import json

from threading import Lock

import faulthandler
faulthandler.enable(open("faultlog.txt", "w"))


import numpy as np

from PySide6.QtWidgets import (
    QApplication, QWidget, QPushButton, QVBoxLayout, QLabel, QListWidget,
    QFileDialog, QHBoxLayout, QSlider, QMessageBox, QListWidgetItem,
    QSpinBox, QComboBox, QCheckBox
)
from PySide6.QtGui import QPixmap, QImage, QIcon, QKeySequence, QShortcut
from PySide6.QtCore import Qt, QTimer, QThread, Signal, QSize


# 1) Camera search dialog popup
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

# 2) Camera search thread (non-blocking)
class CameraSearchThread(QThread):
    cameras_found = Signal(list)

    def run(self):
        found_cameras = []
        for i in range(5):  
            cap = cv2.VideoCapture(i)
            if cap.isOpened():
                found_cameras.append(i)
                cap.release()
            else:
                # Stop searching on first failure
                break
        self.cameras_found.emit(found_cameras)
class CameraOpenThread(QThread):
    # In CameraOpenThread
    camera_opened = Signal(bool, int, object)  # success, index, cap
  # success flag, camera index

    def __init__(self, index):
        super().__init__()
        self.index = index
        self.cap = None

    def run(self):
        cap = cv2.VideoCapture(self.index)
        success = cap.isOpened()

        if not success:
            cap.release()
            cap = None

        self.cap = cap
        self.camera_opened.emit(success, self.index, cap if success else None)

        # Only release if it failed
        if not success and self.cap:
            self.cap.release()
            self.cap = None

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


class StopMotionApp(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("CN Stop Motion App by Sensei Jesse")

        self.project_path = ""
        self.captured_frames = []
        self.undo_stack = []
        self.redo_stack = []
        self.camera_search_thread = None
        self.is_playback_mode = False   
        self.current_camera_index = 0
        self.cap = None
        self.camera_open_thread = None
        self.available_cameras = [] 
       
        self.loop_playback = True
        self.unsaved_changes = False

        self.camera_selector = QComboBox()
        self.capture_btn = QPushButton("Capture Frame")
        
        self.capture_btn.setEnabled(False)
        self.project_loading_dialog = None

        self.cap_lock = Lock()

        self.video_label = QLabel()
        self.video_label.setFixedSize(640, 480)

        self.timeline = QListWidget()
        self.timeline.setFixedHeight(100)
        self.timeline.itemClicked.connect(self.preview_selected_frame)
        self.timeline.setViewMode(QListWidget.IconMode)
        self.timeline.setMovement(QListWidget.Static)
        self.timeline.setSpacing(5)
        self.timeline.setIconSize(QSize(100, 80))  # optional: fixed icon size
        self.timeline.setFlow(QListWidget.LeftToRight)
        self.timeline.setResizeMode(QListWidget.Adjust)
        self.timeline.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOn)
        self.timeline.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.timeline.setWrapping(False)


        self.capture_btn.clicked.connect(self.capture_frame)
        self.capture_btn.setToolTip("Take a snapshot from the live feed")

        self.delete_btn = QPushButton("Delete Frame")
        self.delete_btn.clicked.connect(self.delete_frame)
        self.delete_btn.setToolTip("Remove selected frame")
        self.duplicate_btn = QPushButton("Duplicate Frame")
        self.duplicate_btn.clicked.connect(self.duplicate_frame)
        self.duplicate_btn.setToolTip("Make a copy of the selected frame")

        self.undo_btn = QPushButton("Undo")
        self.undo_btn.clicked.connect(self.undo)
        self.undo_btn.setToolTip("Undo last action")

        self.redo_btn = QPushButton("Redo")
        self.redo_btn.clicked.connect(self.redo)
        self.redo_btn.setToolTip("Redo last undone action")

        self.save_btn = QPushButton("Save Project")
        self.save_btn.clicked.connect(self.save_project)
        self.save_btn.setToolTip("Save current project")

        self.open_btn = QPushButton("Open Project")
        self.open_btn.clicked.connect(self.open_project)
        self.open_btn.setToolTip("Load existing project")

        self.new_project_btn = QPushButton("New Project")
        self.new_project_btn.clicked.connect(self.new_project)
        self.new_project_btn.setToolTip("Start a new project")

        self.play_pause_btn = QPushButton("Play")
        self.play_pause_btn.setCheckable(True)
        self.play_pause_btn.toggled.connect(self.play_pause_toggle)
        self.play_pause_btn.setToolTip("Play/Pause preview")
        self.back_to_live_btn = QPushButton("Back to Live Feed")
        self.back_to_live_btn.clicked.connect(self.resume_live_feed)

        self.loop_checkbox = QCheckBox("Loop")
        self.loop_checkbox.setChecked(True)
        self.loop_checkbox.stateChanged.connect(self.toggle_loop)

        self.export_btn = QPushButton("Export MP4")
        self.export_btn.clicked.connect(self.export_mp4)

        self.export_gif_btn = QPushButton("Export GIF")
        self.export_gif_btn.clicked.connect(self.export_gif)

        self.opacity_slider = QSlider(Qt.Horizontal)
        self.opacity_slider.setRange(0, 100)
        self.opacity_slider.setValue(50)
        self.opacity_slider.valueChanged.connect(self.update_onion_skin)

        self.fps_spin = QSpinBox()
        self.fps_spin.setRange(1, 60)
        self.fps_spin.setValue(12)
        self.onion_layer_spin = QSpinBox()
        self.onion_layer_spin.setRange(1, 10)
        self.onion_layer_spin.setValue(3)
        self.onion_layer_spin.setToolTip("Number of onion skin layers to display")

        layout = QVBoxLayout()

        camera_layout = QHBoxLayout()
        camera_layout.addWidget(QLabel("Select Camera:"))
        camera_layout.addWidget(self.camera_selector)
        layout.addLayout(camera_layout)

        layout.addWidget(self.video_label)

        controls = QHBoxLayout()
        controls.addWidget(self.capture_btn)
        controls.addWidget(self.delete_btn)
        controls.addWidget(self.duplicate_btn)
        controls.addWidget(self.undo_btn)
        controls.addWidget(self.redo_btn)
        controls.addWidget(self.new_project_btn)
        controls.addWidget(self.save_btn)
        controls.addWidget(self.open_btn)
        controls.addWidget(self.play_pause_btn)
        controls.addWidget(self.loop_checkbox)
        controls.addWidget(QLabel("FPS:"))
        controls.addWidget(self.fps_spin)
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
        self.onion_checkbox = QCheckBox("Onion Skin")
        self.onion_checkbox.setChecked(True)
        onion_layout.addWidget(self.onion_checkbox)
        layout.addLayout(onion_layout)


        self.setLayout(layout)
        self.camera_selector.currentIndexChanged.connect(self.change_camera)
        self.timer = QTimer()
        self.timer.timeout.connect(self.update_frame)
        self.timer.start(30)
        self.autosave_timer = QTimer()
        self.autosave_timer.timeout.connect(self.save_project)
        self.autosave_timer.start(300_000)  # Every 5 minutes

        self.playback_timer = QTimer()
        self.playback_timer.timeout.connect(self.playback_next_frame)

        self.playback_index = 0
        QTimer.singleShot(500, self.start_camera_search)  # Wait 100ms to allow UI to show first

       

        QShortcut(QKeySequence("Ctrl+Z"), self).activated.connect(self.undo)
        QShortcut(QKeySequence("Ctrl+Shift+Z"), self).activated.connect(self.redo)
   
    def start_camera_search(self):
        # Avoid starting if thread is still running
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


    def on_cameras_found(self, cameras):
        if self.camera_search_dialog:
            self.camera_search_dialog.close()
            self.camera_search_dialog = None

        # Update combo box only with cameras excluding current opened (if any)
        self.available_cameras = cameras
        self.camera_selector.clear()

        if not cameras:
            self.camera_selector.addItem("No Camera Found")
            self.capture_btn.setEnabled(False)
            QMessageBox.warning(self, "No Cameras", "No cameras were found.")
        else:
            for idx in cameras:
                self.camera_selector.addItem(f"Camera {idx}", idx)

            # If no camera was opened earlier or current cam not in list, open first found
            if self.cap is None or self.current_camera_index not in cameras:
                self.current_camera_index = cameras[0]

                self.open_camera(cameras[0])


    def open_camera(self, index):
        with self.cap_lock:
            if self.cap and self.cap.isOpened() and self.current_camera_index == index:
                print("Camera already open and matches requested index.")
                return

        # Instead of forcibly quitting and waiting, just skip starting a new thread if one is running
        if self.camera_open_thread and self.camera_open_thread.isRunning():
            print("Camera open thread still running, ignoring open request")
            return

        self.camera_selector.setEnabled(False)
        self.capture_btn.setEnabled(False)
        self.video_label.setText("Loading camera feed...")
        self.video_label.setAlignment(Qt.AlignCenter)


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



    def on_camera_opened(self, success, index, cap):
        self.camera_selector.setEnabled(True)
        if self.project_loading_dialog:
            self.project_loading_dialog.close()
            self.project_loading_dialog = None

        if success and cap:
            with self.cap_lock:
                if self.cap:
                    self.cap.release()
                self.cap = cap

            self.capture_btn.setEnabled(True)
            self.current_camera_index = index

            # Stop playback timer if running, sync UI
            if self.playback_timer.isActive():
                self.playback_timer.stop()
                self.play_pause_btn.setChecked(False)

            if not self.timer.isActive():
                self.timer.start(30)

        else:
            if index == 0:
                self.start_camera_search()
            else:
                QMessageBox.warning(self, "Camera Error", f"Failed to open camera {index}")
            self.capture_btn.setEnabled(False)

    def preview_selected_frame(self, item):
        self.timer.stop()
        frame_path = item.data(Qt.UserRole)
        frame = cv2.imread(frame_path)


        if isinstance(frame, np.ndarray):
            height, width, channel = frame.shape
            bytes_per_line = 3 * width
            q_img = QImage(frame.data, width, height, bytes_per_line, QImage.Format_RGB888).rgbSwapped()
            self.video_label.setPixmap(QPixmap.fromImage(q_img))
        else:
            print("Warning: Expected image data but got something else")



    def update_frame(self):
        if self.is_playback_mode:
            return  # Don't update live feed while playing back

        with self.cap_lock:
            if not self.cap:
                return

            ret, frame = self.cap.read()
            if not ret or frame is None:
                print("Frame read failed, resuming live feed...")
                self.cap.release()
                self.cap = None
                QTimer.singleShot(1000, self.resume_live_feed)
                return

            self.latest_frame = frame.copy()

        # Now that we're outside the lock, process the frame
        if self.onion_checkbox.isChecked() and self.captured_frames:
            self.update_onion_skin()
        else:
            frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            h, w, ch = frame.shape
            bytes_per_line = ch * w
            qt_image = QImage(frame.data, w, h, bytes_per_line, QImage.Format_RGB888)
            pix = QPixmap.fromImage(qt_image).scaled(
                self.video_label.width(), self.video_label.height(), Qt.KeepAspectRatio
            )
            self.video_label.setPixmap(pix)
            if self.video_label.text():
                self.video_label.setText("")

    def resume_live_feed(self):
        # Stop playback timer if running
        if self.playback_timer.isActive():
            self.playback_timer.stop()
            self.play_pause_btn.setChecked(False)  # keep UI synced

        with self.cap_lock:
            if self.cap and self.cap.isOpened():
                # Start live feed timer if not running
                if not self.timer.isActive():
                    self.timer.start(30)
            else:
                # Open camera asynchronously
                self.open_camera(self.current_camera_index)



    def capture_frame(self):
        if self.latest_frame is None:
            QMessageBox.warning(self, "Capture Failed", "No frame available to capture.")
            return

        if not self.project_path:
            QMessageBox.warning(self, "No Project", "Please create a new project before capturing frames.")
            return

        frame = self.latest_frame
        self.unsaved_changes = True

        frame_name = f"frame_{len(self.captured_frames):04d}.png"
        frame_path = os.path.join(self.project_path, frame_name)
        cv2.imwrite(frame_path, frame)

        index = len(self.captured_frames)  # new frame will be appended at this index
        self.captured_frames.append(frame_path)

        # Push action as (type, index, path)
        self.undo_stack.append(("add", index, frame_path))
        self.redo_stack.clear()  # Clear redo stack on new action

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

        undo_cache_dir = os.path.join(self.project_path, ".undo_cache")
        os.makedirs(undo_cache_dir, exist_ok=True)

        for item in selected_items:
            row = self.timeline.row(item)
            path = self.captured_frames.pop(row)

            if os.path.exists(path):
                filename = os.path.basename(path)
                backup_path = os.path.join(undo_cache_dir, filename)
                try:
                    shutil.move(path, backup_path)
                except Exception as e:
                    print(f"Failed to move {path} to undo cache: {e}")
                    # If move failed, put back the path into captured_frames
                    self.captured_frames.insert(row, path)
                    continue
            else:
                backup_path = path  # File already missing, but keep undo info

            self.undo_stack.append(("delete", backup_path, row))

        self.unsaved_changes = True
        self.refresh_timeline()
        self.resume_live_feed()


    def refresh_timeline(self):
        self.timeline.clear()
        icon_size = 80
        valid_frames = []

        for idx, frame_path in enumerate(self.captured_frames):
            if not os.path.exists(frame_path):
                print(f"Missing file: {frame_path}")
                continue

            frame = cv2.imread(frame_path)
            if frame is None:
                print(f"Unreadable image file: {frame_path}")
                continue

            try:
                frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            except cv2.error as e:
                print(f"OpenCV error on frame {frame_path}: {e}")
                continue

            h, w, ch = frame.shape
            bytes_per_line = ch * w
            q_img = QImage(frame.data, w, h, bytes_per_line, QImage.Format_RGB888)
            if q_img.isNull():
                print(f"Null QImage from frame: {frame_path}")
                continue

            thumb = QPixmap.fromImage(q_img).scaledToHeight(icon_size, Qt.SmoothTransformation)

            item = QListWidgetItem(QIcon(thumb), f"{len(valid_frames)}")
            item.setData(Qt.UserRole, frame_path)
            item.setSizeHint(QSize(icon_size + 10, icon_size + 20))
            self.timeline.addItem(item)

            valid_frames.append(frame_path)

        self.captured_frames = valid_frames

    def undo(self):
        if not self.undo_stack:
            return

        action = self.undo_stack.pop()
        self.redo_stack.append(action)

        if action[0] == "add":
            # Undo adding a frame: remove file & path
            self.unsaved_changes = True
            if action[1] in self.captured_frames:
                self.captured_frames.remove(action[1])
                if os.path.exists(action[1]):
                    os.remove(action[1])

        elif action[0] == "delete":
            path = action[1]
            index = action[2]

            # Move file back from undo cache to project folder
            filename = os.path.basename(path)
            original_path = os.path.join(self.project_path, filename)

            if os.path.exists(path):
                try:
                    shutil.move(path, original_path)
                except Exception as e:
                    print(f"Failed to restore {path} during undo: {e}")
                    original_path = path  # fallback to backup path if move fails

            if 0 <= index <= len(self.captured_frames):
                self.captured_frames.insert(index, original_path)

            self.unsaved_changes = True

        self.refresh_timeline()


    def redo(self):
        if not self.redo_stack:
            return

        action = self.redo_stack.pop()
        self.undo_stack.append(action)

        if action[0] == "add":
            # Redo adding frame: just add path back
            self.captured_frames.append(action[1])

        elif action[0] == "delete":
            path = action[1]

            if path in self.captured_frames:
                self.captured_frames.remove(path)

            # Move file back to undo cache to simulate deletion again
            undo_cache_dir = os.path.join(self.project_path, ".undo_cache")
            os.makedirs(undo_cache_dir, exist_ok=True)

            filename = os.path.basename(path)
            backup_path = os.path.join(undo_cache_dir, filename)

            if os.path.exists(path):
                try:
                    shutil.move(path, backup_path)
                except Exception as e:
                    print(f"Failed to move {path} back to undo cache during redo: {e}")

        self.refresh_timeline()


    def update_onion_skin(self):
        frame = self.latest_frame
        if not self.cap or not self.cap.isOpened():
            return

        ret, live_frame = self.cap.read()
        if not ret:
            return

        live_frame = cv2.cvtColor(live_frame, cv2.COLOR_BGR2RGBA)
        height, width = live_frame.shape[:2]

        # Start with live frame
        composite = live_frame.astype(float)

        # Use user-defined number of layers
        max_layers = self.onion_layer_spin.value()
        num_frames = len(self.captured_frames)
        layers_to_show = min(max_layers, num_frames)

        for i in range(1, layers_to_show + 1):
            frame_path = self.captured_frames[-i]
            previous_frame = cv2.imread(frame_path, cv2.IMREAD_UNCHANGED)
            if previous_frame is None:
                continue
            previous_frame = cv2.resize(previous_frame, (width, height))
            previous_frame = cv2.cvtColor(previous_frame, cv2.COLOR_BGR2RGBA)

            base_opacity = self.opacity_slider.value() / 100.0
            layer_opacity = base_opacity / i  # fade with distance

            composite = cv2.addWeighted(previous_frame.astype(float), layer_opacity, composite, 1.0, 0)

        composite = np.clip(composite, 0, 255).astype(np.uint8)
        qt_image = QImage(composite.data, width, height, QImage.Format_RGBA8888)
        pix = QPixmap.fromImage(qt_image).scaled(self.video_label.width(), self.video_label.height(), Qt.KeepAspectRatio)
        self.video_label.setPixmap(pix)

    

    def new_project(self):
        if self.unsaved_changes:
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

            self.project_path = folder
            self.captured_frames.clear()
            self.undo_stack.clear()
            self.redo_stack.clear()
            self.refresh_timeline()
            self.unsaved_changes = False

            undo_cache = os.path.join(folder, ".undo_cache")
            if os.path.exists(undo_cache):
                shutil.rmtree(undo_cache)
            os.makedirs(undo_cache)

            self.open_camera(self.current_camera_index)

            if self.project_loading_dialog:
                self.project_loading_dialog.close()
                self.project_loading_dialog = None

    def toggle_loop(self, state):
        self.loop_playback = bool(state)

    def playback_next_frame(self):
        if not self.captured_frames:
            self.play_pause_btn.setChecked(False)
            self.playback_timer.stop()
            return

        if self.playback_index >= len(self.captured_frames):
            if self.loop_playback:
                self.playback_index = 0
            else:
                self.play_pause_btn.setChecked(False)
                self.playback_timer.stop()
                return

        frame_path = self.captured_frames[self.playback_index]
        if not os.path.exists(frame_path):
            print(f"Frame path does not exist: {frame_path}")
            self.playback_index += 1
            return

        pixmap = QPixmap(frame_path).scaled(self.video_label.width(), self.video_label.height(), Qt.KeepAspectRatio)
        self.video_label.setPixmap(pixmap)
        self.playback_index += 1


    def save_project(self):
        if self.project_path:
            undo_folder = os.path.join(self.project_path, ".undo_cache")
            if os.path.exists(undo_folder):
                shutil.rmtree(undo_folder)
            self.save_metadata()  # Save settings here
            QMessageBox.information(self, "Project Saved", f"Project saved in: {self.project_path}")
            self.unsaved_changes = False


    def open_project(self):
        if self.unsaved_changes:
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

            self.project_path = folder
            self.captured_frames = []
            self.undo_stack.clear()
            self.redo_stack.clear()
            self.unsaved_changes = False

            # Create or clear undo cache folder
            undo_cache_dir = os.path.join(self.project_path, ".undo_cache")
            if os.path.exists(undo_cache_dir):
                try:
                    shutil.rmtree(undo_cache_dir)
                except Exception as e:
                    print(f"Failed to clear undo cache: {e}")
            try:
                os.makedirs(undo_cache_dir)
            except Exception as e:
                print(f"Failed to create undo cache directory: {e}")

            for file in sorted(os.listdir(folder)):
                if file.endswith(".png") and file.startswith("frame_"):
                    full_path = os.path.join(folder, file)
                    if os.path.exists(full_path) and cv2.imread(full_path) is not None:
                        self.captured_frames.append(full_path)
                    else:
                        print(f"Skipping missing or unreadable file: {full_path}")

            self.refresh_timeline()
            self.load_metadata()
            self.open_camera(self.current_camera_index)

            if self.project_loading_dialog:
                self.project_loading_dialog.close()
                self.project_loading_dialog.deleteLater()
                self.project_loading_dialog = None


    def change_camera(self, index):
        selected_index = self.camera_selector.itemData(index)
        if selected_index is not None:
            self.current_camera_index = selected_index
            self.open_camera(self.current_camera_index)

    def play_pause_toggle(self, checked):
        if checked:
            self.play_pause_btn.setText("Pause")
            self.is_playback_mode = True
            self.playback_index = 0
            self.playback_timer.start(int(1000 / self.fps_spin.value()))
        else:
            self.play_pause_btn.setText("Play")
            self.is_playback_mode = False
            self.playback_timer.stop()


    def export_mp4(self):
        if not self.captured_frames:
            QMessageBox.warning(self, "Export Error", "No frames to export!")
            return
        import imageio
        save_path, _ = QFileDialog.getSaveFileName(self, "Save MP4 Video", "", "MP4 files (*.mp4)")
        if not save_path:
            return

        fps = self.fps_spin.value()
        first_frame = cv2.imread(self.captured_frames[0])
        if first_frame is None:
            QMessageBox.warning(self, "Export Error", "Failed to read first frame!")
            return

        height, width, _ = first_frame.shape
        fourcc = cv2.VideoWriter_fourcc(*'mp4v')
        video_writer = cv2.VideoWriter(save_path, fourcc, fps, (width, height))

        for frame_path in self.captured_frames:
            frame = cv2.imread(frame_path, cv2.IMREAD_REDUCED_COLOR_2)

            if frame is not None:
                video_writer.write(frame)
        video_writer.release()

        QMessageBox.information(self, "Export Complete", f"MP4 video saved to:\n{save_path}")

    def export_gif(self):
        if not self.captured_frames:
            QMessageBox.warning(self, "Export Error", "No frames to export!")
            return

        import imageio

        save_path, _ = QFileDialog.getSaveFileName(self, "Save GIF Animation", "", "GIF files (*.gif)")
        if not save_path:
            return  # User cancelled

        fps = self.fps_spin.value()
        duration = 1 / fps

        images = []
        bad_frames = []
        for frame_path in self.captured_frames:
            try:
                img = imageio.imread(frame_path)
                images.append(img)
            except Exception as e:
                bad_frames.append(frame_path)
                print(f"Warning: Could not load frame {frame_path}: {e}")

        if not images:
            QMessageBox.warning(self, "Export Error", "No valid frames to export.")
            return

        try:
            imageio.mimsave(save_path, images, duration=duration)
        except Exception as e:
            QMessageBox.critical(self, "Export Failed", f"Could not save GIF:\n{e}")
            return

        if bad_frames:
            QMessageBox.warning(
                self,
                "Partial Export",
                f"Some frames could not be loaded and were skipped:\n\n" + "\n".join(bad_frames)
            )
        else:
            QMessageBox.information(self, "Export Complete", f"GIF animation saved to:\n{save_path}")

    def save_metadata(self):
        if not self.project_path:
            return

        metadata = {
            "fps": self.fps_spin.value(),
            "onion_opacity": self.opacity_slider.value(),
            "onion_layers": self.onion_layer_spin.value(),
            "loop_playback": self.loop_checkbox.isChecked()
        }

        meta_path = os.path.join(self.project_path, "project_meta.json")
        try:
            with open(meta_path, "w") as f:
                json.dump(metadata, f, indent=2)
        except Exception as e:
            print(f"Failed to save metadata: {e}")

    def load_metadata(self):
        if not self.project_path:
            return

        meta_path = os.path.join(self.project_path, "project_meta.json")
        if not os.path.exists(meta_path):
            return

        try:
            with open(meta_path, "r") as f:
                metadata = json.load(f)

            self.fps_spin.setValue(metadata.get("fps", 12))
            self.opacity_slider.setValue(metadata.get("onion_opacity", 50))
            self.onion_layer_spin.setValue(metadata.get("onion_layers", 3))
            self.loop_checkbox.setChecked(metadata.get("loop_playback", True))

        except Exception as e:
            print(f"Failed to load metadata: {e}")
    def duplicate_frame(self):
        selected_items = self.timeline.selectedItems()
        if not selected_items:
            QMessageBox.information(self, "No Frame Selected", "Please select a frame to duplicate.")
            return

        for item in selected_items:
            index = self.timeline.row(item)
            original_path = self.captured_frames[index]

            if not os.path.exists(original_path):
                QMessageBox.warning(self, "Error", f"Original frame is missing:\n{original_path}")
                continue

            # Generate new frame filename
            new_index = len(self.captured_frames)
            new_name = f"frame_{new_index:04d}.png"
            new_path = os.path.join(self.project_path, new_name)

            # Copy the frame file
            try:
                shutil.copy(original_path, new_path)
            except Exception as e:
                QMessageBox.critical(self, "Duplicate Failed", f"Could not copy frame:\n{e}")
                continue

            # Insert the copy after the selected frame
            insert_at = index + 1
            self.captured_frames.insert(insert_at, new_path)
            self.undo_stack.append(("add", new_path))
            self.unsaved_changes = True

        self.refresh_timeline()
        self.timeline.scrollToBottom()

    def closeEvent(self, event):
        if self.unsaved_changes:
            reply = QMessageBox.question(
                self, "Unsaved Changes",
                "You have unsaved changes. Are you sure you want to quit?",
                QMessageBox.Yes | QMessageBox.No
            )
            if reply == QMessageBox.No:
                event.ignore()
                return

        print("Closing app...")

        if self.camera_open_thread:
            if self.camera_open_thread.isRunning():
                print("Waiting for camera thread to finish...")
                self.camera_open_thread.quit()
                self.camera_open_thread.wait()

            self.camera_open_thread.deleteLater()
            self.camera_open_thread = None

        with self.cap_lock:
            if self.cap:
                print("Releasing camera...")
                self.cap.release()
                self.cap = None

        self.timer.stop()
        self.playback_timer.stop()

        print("Closed cleanly.")
        event.accept()


if __name__ == "__main__":
    import traceback

    try:
        app = QApplication(sys.argv)
        window = StopMotionApp()
        window.show()
        sys.exit(app.exec())
    except Exception as e:
        with open("crashlog.txt", "w") as f:
            traceback.print_exc(file=f)