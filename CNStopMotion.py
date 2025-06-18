import sys
import os
import cv2
import shutil
import imageio
import numpy as np

from PySide6.QtWidgets import (
    QApplication, QWidget, QPushButton, QVBoxLayout, QLabel, QListWidget,
    QFileDialog, QHBoxLayout, QSlider, QMessageBox, QListWidgetItem,
    QSpinBox, QComboBox, QCheckBox
)
from PySide6.QtGui import QPixmap, QImage, QIcon, QKeySequence, QShortcut
from PySide6.QtCore import Qt, QTimer

from PySide6.QtCore import QThread, Signal
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
        for i in range(20):  # max 20 to be safe, but you can set a lower max
            cap = cv2.VideoCapture(i)
            if cap.isOpened():
                found_cameras.append(i)
                cap.release()
            else:
                # Stop searching on first failure
                break
        self.cameras_found.emit(found_cameras)
class CameraOpenThread(QThread):
    camera_opened = Signal(bool, int)  # success flag, camera index

    def __init__(self, index):
        super().__init__()
        self.index = index
        self.cap = None

    def run(self):
        cap = cv2.VideoCapture(self.index)
        success = cap.isOpened()
        cap.release()

            # emit result to main thread
        self.camera_opened.emit(success, self.index)
            # store cap only if needed (see note)
        self.cap = cap

class StopMotionApp(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("CN Stop Motion App by Sensei Jesse")

        self.project_path = ""
        self.captured_frames = []
        self.undo_stack = []
        self.redo_stack = []
        self.camera_search_thread = None
        self.camera_open_thread = None
        self.current_camera_index = 0
        self.cap = None
        self.camera_open_thread = None
        self.available_cameras = [] 
       
        self.loop_playback = True

        self.camera_selector = QComboBox()
        self.capture_btn = QPushButton("Capture Frame")
        self.open_camera(0)
        self.capture_btn.setEnabled(False)
      
        self.camera_selector.currentIndexChanged.connect(self.change_camera)

        self.video_label = QLabel()
        self.video_label.setFixedSize(640, 480)

        self.timeline = QListWidget()
        self.timeline.setFixedHeight(100)
        self.timeline.itemClicked.connect(self.preview_selected_frame)

        self.capture_btn.clicked.connect(self.capture_frame)
        self.capture_btn.setToolTip("Take a snapshot from the live feed")

        self.delete_btn = QPushButton("Delete Frame")
        self.delete_btn.clicked.connect(self.delete_frame)
        self.delete_btn.setToolTip("Remove selected frame")

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

        self.playback_timer = QTimer()
        self.playback_timer.timeout.connect(self.playback_next_frame)

        self.playback_index = 0
        self.start_camera_search()
       

        QShortcut(QKeySequence("Ctrl+Z"), self).activated.connect(self.undo)
        QShortcut(QKeySequence("Ctrl+Shift+Z"), self).activated.connect(self.redo)
    # In your StopMotionApp, modify start_camera_search and open_camera logic:

    def start_camera_search(self):
        # Only start if no active search thread running
        if self.camera_search_thread and self.camera_search_thread.isRunning():
            return

        self.camera_search_dialog = CameraSearchDialog(self)
        self.camera_search_dialog.show()

        self.camera_search_thread = CameraSearchThread()
        self.camera_search_thread.cameras_found.connect(self.on_cameras_found)
        self.camera_search_thread.finished.connect(self.camera_search_thread.deleteLater)
        self.camera_search_thread.start()

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
                self.open_camera(cameras[0])

    def open_camera(self, index):
        if self.cap:
            self.cap.release()
            self.cap = None

        if self.camera_open_thread and self.camera_open_thread.isRunning():
            self.camera_open_thread.quit()
            self.camera_open_thread.wait()
            self.camera_open_thread = None

        self.camera_selector.setEnabled(False)
        self.capture_btn.setEnabled(False)

        self.camera_open_thread = CameraOpenThread(index)
        self.camera_open_thread.camera_opened.connect(self.on_camera_opened)
        self.camera_open_thread.finished.connect(self.camera_open_thread.deleteLater)
        self.camera_open_thread.start()

    def on_camera_opened(self, success, index):
        self.camera_selector.setEnabled(True)
        if success:
            self.cap = cv2.VideoCapture(index)
            self.capture_btn.setEnabled(True)
            self.current_camera_index = index
        else:
            # If camera 0 failed on app start, try start search now
            if index == 0:
                self.start_camera_search()
            else:
                QMessageBox.warning(self, "Camera Error", f"Failed to open camera {index}")
                self.capture_btn.setEnabled(False)
    def preview_selected_frame(self, item):
        self.timer.stop()
        frame = item.data(Qt.UserRole)

        if isinstance(frame, np.ndarray):
            height, width, channel = frame.shape
            bytes_per_line = 3 * width
            q_img = QImage(frame.data, width, height, bytes_per_line, QImage.Format_RGB888).rgbSwapped()
            self.video_label.setPixmap(QPixmap.fromImage(q_img))
        else:
            print("Warning: Expected image data but got something else")



    def update_frame(self):
        if not self.cap:
            return  # no live feed, do nothing

        ret, frame = self.cap.read()
        if not ret:
            return

        if self.onion_checkbox.isChecked() and self.captured_frames:
            self.update_onion_skin()
        else:
            frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            h, w, ch = frame.shape
            bytes_per_line = ch * w
            qt_image = QImage(frame.data, w, h, bytes_per_line, QImage.Format_RGB888)
            pix = QPixmap.fromImage(qt_image).scaled(self.video_label.width(), self.video_label.height(), Qt.KeepAspectRatio)
            self.video_label.setPixmap(pix)
    def resume_live_feed(self):
        if self.cap and self.cap.isOpened():
            if not self.timer.isActive():
                self.timer.start(30)


    def capture_frame(self):
        if not self.cap or not self.cap.isOpened():
            return

        if not self.project_path:
            QMessageBox.warning(self, "No Project", "Please create a new project before capturing frames.")
            return

        ret, frame = self.cap.read()
        if ret:
            frame_name = f"frame_{len(self.captured_frames):04d}.png"
            frame_path = os.path.join(self.project_path, frame_name)
            cv2.imwrite(frame_path, frame)
            self.captured_frames.append(frame_path)
            self.undo_stack.append(("add", frame_path))
            self.refresh_timeline()


    def delete_frame(self):
        selected_items = self.timeline.selectedItems()
        if not selected_items:
            return
        for item in selected_items:
            row = self.timeline.row(item)
            path = self.captured_frames.pop(row)
            self.undo_stack.append(("delete", path, row))
            if os.path.exists(path):
                os.remove(path)
        self.refresh_timeline()

    def refresh_timeline(self):
        self.timeline.clear()
        for idx, frame_path in enumerate(self.captured_frames):
            item = QListWidgetItem()
            
            # Load the image for thumbnail and data storage
            frame = cv2.imread(frame_path)
            if frame is None:
                continue  # Skip if image failed to load
            
            # Convert to Qt image for thumbnail
            height, width, channel = frame.shape
            bytes_per_line = 3 * width
            q_img = QImage(frame.data, width, height, bytes_per_line, QImage.Format_RGB888).rgbSwapped()
            thumb = QPixmap.fromImage(q_img).scaledToHeight(80)

            item.setIcon(QIcon(thumb))
            item.setText(f"Frame {idx}")
            item.setData(Qt.UserRole, frame)  # Store the actual image (numpy array)

            self.timeline.addItem(item)



    def undo(self):
        if not self.undo_stack:
            return
        action = self.undo_stack.pop()
        self.redo_stack.append(action)
        if action[0] == "add":
            self.captured_frames.remove(action[1])
            if os.path.exists(action[1]):
                os.remove(action[1])
        elif action[0] == "delete":
            self.captured_frames.insert(action[2], action[1])
        self.refresh_timeline()

    def redo(self):
        if not self.redo_stack:
            return
        action = self.redo_stack.pop()
        self.undo_stack.append(action)
        if action[0] == "add":
            self.captured_frames.append(action[1])
        elif action[0] == "delete":
            self.captured_frames.remove(action[1])
            if os.path.exists(action[1]):
                os.remove(action[1])
        self.refresh_timeline()

    def update_onion_skin(self):
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
        folder = QFileDialog.getExistingDirectory(self, "Create New Project Folder")
        if folder:
            self.project_path = folder
            self.captured_frames.clear()
            self.undo_stack.clear()
            self.redo_stack.clear()
            self.refresh_timeline()
            undo_cache = os.path.join(folder, ".undo_cache")
            if os.path.exists(undo_cache):
                shutil.rmtree(undo_cache)
            os.makedirs(undo_cache)

    def toggle_loop(self, state):
        self.loop_playback = bool(state)

    def playback_next_frame(self):
        if not self.captured_frames:
            self.play_pause_btn.setChecked(False)
            self.playback_timer.stop()
            return
        frame_path = self.captured_frames[self.playback_index]
        pixmap = QPixmap(frame_path).scaled(self.video_label.width(), self.video_label.height(), Qt.KeepAspectRatio)
        self.video_label.setPixmap(pixmap)
        self.playback_index += 1
        if self.playback_index >= len(self.captured_frames):
            if self.loop_playback:
                self.playback_index = 0
            else:
                self.play_pause_btn.setChecked(False)

    def save_project(self):
        if self.project_path:
            undo_folder = os.path.join(self.project_path, ".undo_cache")
            if os.path.exists(undo_folder):
                shutil.rmtree(undo_folder)
            QMessageBox.information(self, "Project Saved", f"Project saved in: {self.project_path}")

    def open_project(self):
        folder = QFileDialog.getExistingDirectory(self, "Open Project Folder")
        if folder:
            self.project_path = folder
            self.captured_frames = []
            for file in sorted(os.listdir(folder)):
                if file.endswith(".png") and file.startswith("frame_"):
                    self.captured_frames.append(os.path.join(folder, file))
            self.refresh_timeline()
    def change_camera(self, index):
        selected_index = self.camera_selector.itemData(index)
        if selected_index is not None:
            self.current_camera_index = selected_index
            self.open_camera(self.current_camera_index)

    def play_pause_toggle(self, checked):
        if checked:
            self.play_pause_btn.setText("Pause")

            # Stop live camera capture to disable live preview
            if self.cap:
                self.cap.release()
                self.cap = None

            if not self.captured_frames:
                QMessageBox.warning(self, "Playback", "No frames to play.")
                self.play_pause_btn.setChecked(False)
                return

            self.playback_index = 0
            self.playback_timer.start(int(1000 / self.fps_spin.value()))

        else:
            self.play_pause_btn.setText("Play")
            self.playback_timer.stop()

            # Restart live camera feed when playback stops
            if self.current_camera_index is not None:
                self.open_camera(self.current_camera_index)


    def export_mp4(self):
        if not self.captured_frames:
            QMessageBox.warning(self, "Export Error", "No frames to export!")
            return

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
            frame = cv2.imread(frame_path)
            if frame is not None:
                video_writer.write(frame)
        video_writer.release()

        QMessageBox.information(self, "Export Complete", f"MP4 video saved to:\n{save_path}")

    def export_gif(self):
        if not self.captured_frames:
            QMessageBox.warning(self, "Export Error", "No frames to export!")
            return

        save_path, _ = QFileDialog.getSaveFileName(self, "Save GIF Animation", "", "GIF files (*.gif)")
        if not save_path:
            return

        fps = self.fps_spin.value()
        duration = 1 / fps

        images = []
        for frame_path in self.captured_frames:
            img = imageio.imread(frame_path)
            images.append(img)

        imageio.mimsave(save_path, images, duration=duration)

        QMessageBox.information(self, "Export Complete", f"GIF animation saved to:\n{save_path}")
    def closeEvent(self, event):
        if self.camera_open_thread and self.camera_open_thread.isRunning():
            self.camera_open_thread.quit()
            self.camera_open_thread.wait()
        event.accept()

if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = StopMotionApp()
    window.show()
    sys.exit(app.exec())
