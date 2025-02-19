import os
import sys
from flask import Flask, Response, render_template, jsonify, send_from_directory, request
from werkzeug.utils import secure_filename
import cv2
import mediapipe as mp
import numpy as np
import logging
import time
from flask_socketio import SocketIO, emit
from camera.manager import CameraManager
from pose.drawer import PoseDrawer  # 确保从正确的路径导入
from connect.pose_sender import PoseSender
from connect.socket_manager import SocketManager
from config import settings
from config.settings import CAMERA_CONFIG, POSE_CONFIG
from audio.processor import AudioProcessor
from pose.pose_binding import PoseBinding
from pose.detector import PoseDetector
from pose.types import PoseData
from face.face_verification import FaceVerifier
# from connect.jitsi.transport import JitsiTransport
# from connect.jitsi.meeting_manager import JitsiMeetingManager
# from config.jitsi_config import JITSI_CONFIG
import asyncio
import absl.logging

# 抑制 TensorFlow 警告
os.environ['TF_CPP_MIN_LOG_LEVEL'] = '2'  # 0=all, 1=INFO, 2=WARNING, 3=ERROR
logging.getLogger('tensorflow').setLevel(logging.ERROR)
absl.logging.set_verbosity(absl.logging.ERROR)

# 禁用 mediapipe 的调试日志
logging.getLogger('mediapipe').setLevel(logging.ERROR)

# 配置日志格式
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# 获取项目根目录的绝对路径
project_root = os.path.dirname(os.path.abspath(__file__))
template_dir = os.path.join(project_root, 'frontend', 'pages')
static_dir = os.path.join(project_root, 'frontend', 'static')

app = Flask(__name__, 
           template_folder=template_dir,
           static_folder=static_dir,
           static_url_path='/static')

# 初始化音频处理器
audio_processor = AudioProcessor()

# 定义上传文件夹路径
UPLOAD_FOLDER = os.path.join(project_root, 'uploads')

# 初始化 Socket.IO
socketio = SocketIO(app, cors_allowed_origins="*")
socket_manager = SocketManager(socketio, audio_processor)
pose_sender = PoseSender(config=POSE_CONFIG)

# MediaPipe 初始化
mp_pose = mp.solutions.pose
mp_hands = mp.solutions.hands
mp_drawing = mp.solutions.drawing_utils
mp_drawing_styles = mp.solutions.drawing_styles
mp_face_mesh = mp.solutions.face_mesh

# 初始化 MediaPipe 模型
pose = mp_pose.Pose(
    static_image_mode=False,
    model_complexity=2,
    enable_segmentation=True,
    smooth_landmarks=True,
    min_detection_confidence=0.5,
    min_tracking_confidence=0.5
)

hands = mp_hands.Hands(
    static_image_mode=False,
    max_num_hands=2,
    min_detection_confidence=0.5,
    min_tracking_confidence=0.5
)

face_mesh = mp_face_mesh.FaceMesh(
    static_image_mode=False,
    max_num_faces=1,
    min_detection_confidence=0.5,
    min_tracking_confidence=0.5
)

# 全局变量
camera_manager = CameraManager(config=CAMERA_CONFIG)
pose_drawer = PoseDrawer()
pose_binding = PoseBinding()
initial_frame = None
initial_regions = None

# 初始化处理器
audio_processor = AudioProcessor()
audio_processor.set_socketio(socketio)

# 初始化检测器
pose_detector = PoseDetector()

# 在全局变量部分添加
REFERENCE_DIR = os.path.join(project_root, 'output', 'reference')
os.makedirs(REFERENCE_DIR, exist_ok=True)

from pose.initial_manager import InitialFrameManager
initial_manager = InitialFrameManager(os.path.join(project_root, 'output'))

def check_camera_settings(cap):
    """检查摄像头实际参数"""
    logger.info("摄像头当前参数:")
    params = {
        cv2.CAP_PROP_EXPOSURE: "曝光值",
        cv2.CAP_PROP_BRIGHTNESS: "亮度",
        cv2.CAP_PROP_CONTRAST: "对比度",
        cv2.CAP_PROP_GAIN: "增益"
    }
    
    for param, name in params.items():
        value = cap.get(param)
        logger.info(f"{name}: {value}")

@app.route('/')
def index():
    """渲染显示页面"""
    return render_template('display.html')

@app.route('/start_capture', methods=['POST'])
def start_capture():
    """启动摄像头"""
    try:
        success = camera_manager.start()
        return jsonify({'success': success})
    except Exception as e:
        logger.error(f"启动摄像头失败: {str(e)}")
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/stop_capture', methods=['POST'])
def stop_capture():
    """停止摄像头"""
    try:
        success = camera_manager.stop()
        return jsonify({'success': success})
    except Exception as e:
        logger.error(f"停止摄像头失败: {str(e)}")
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/video_feed')
def video_feed():
    """视频流路由"""
    return Response(
        generate_frames(),
        mimetype='multipart/x-mixed-replace; boundary=frame'
    )

@app.route('/start_audio', methods=['POST'])
def start_audio():
    success = audio_processor.start_recording()
    return jsonify({'success': success})

@app.route('/stop_audio', methods=['POST'])
def stop_audio():
    success = audio_processor.stop_recording()
    return jsonify({'success': success})

@app.route('/check_stream_status')
def check_stream_status():
    try:
        status = {
            'video': {
                'is_streaming': camera_manager.is_running,
                'fps': camera_manager.current_fps
            },
            'audio': {
                'is_recording': audio_processor.is_recording,
                'sample_rate': audio_processor.sample_rate,
                'buffer_size': len(audio_processor.frames) if hasattr(audio_processor, 'frames') else 0
            }
        }
        return jsonify(status), 200
    except Exception as e:
        logger.error(f"获取流状态失败: {str(e)}")
        return jsonify({'error': str(e)}), 500

@app.route('/capture_initial', methods=['POST'])
def capture_initial():
    """捕获初始参考帧"""
    try:
        # 1. 检查相机状态
        if not camera_manager.is_running:
            return jsonify({
                'success': False, 
                'error': 'Camera is not running'
            }), 400
            
        # 2. 捕获图像
        success, frame = camera_manager.read()
        if not success or frame is None:
            return jsonify({
                'success': False,
                'error': 'Failed to capture frame'
            }), 500
            
        # 3. 检测姿态
        frame_rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        pose_results = pose.process(frame_rgb)
        
        if not pose_results or not pose_results.pose_landmarks:
            return jsonify({
                'success': False,
                'error': 'No pose detected'
            }), 400
            
        # 4. 准备姿态数据
        try:
            keypoints = PoseDetector.mediapipe_to_keypoints(pose_results.pose_landmarks)
            pose_data = PoseData(
                keypoints=keypoints,
                timestamp=time.time(),
                confidence=1.0
            )
        except Exception as e:
            logger.error(f"处理关键点失败: {e}")
            return jsonify({
                'success': False,
                'error': f'Failed to process keypoints: {str(e)}'
            }), 500
            
        # 5. 保存参考帧
        success, result = initial_manager.save_initial_frame(frame, pose_data)
        if not success:
            return jsonify({
                'success': False,
                'error': f'Failed to save reference frame: {result}'
            }), 500
            
        # 6. 创建区域绑定
        try:
            initial_regions = pose_binding.create_binding(frame, pose_data)
        except Exception as e:
            logger.error(f"创建区域绑定失败: {e}")
            # 继续执行，不影响参考帧的保存
            
        return jsonify({
            'success': True,
            'timestamp': pose_data.timestamp,
            'path': result,
            'frame_size': {
                'width': frame.shape[1],
                'height': frame.shape[0]
            }
        })
        
    except Exception as e:
        logger.error(f"捕获初始帧失败: {str(e)}", exc_info=True)
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500

@app.route('/reference_status', methods=['GET'])
def get_reference_status():
    """获取参考帧状态"""
    try:
        status = initial_manager.get_status()
        return jsonify({
            'success': True,
            **status
        })
    except Exception as e:
        logger.error(f"获取参考帧状态失败: {str(e)}")
        return jsonify({'success': False, 'error': str(e)}), 500

def generate_frames():
    """生成视频帧"""
    while True:
        if not camera_manager.is_running:
            time.sleep(0.1)
            continue
            
        frame = camera_manager.read_frame()
        if frame is None:
            continue
            
        # 转换颜色空间
        frame_rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        
        try:
            # 处理姿态
            pose_results = pose.process(frame_rgb)
            # 处理手部
            hands_results = hands.process(frame_rgb)
            # 处理面部
            face_results = face_mesh.process(frame_rgb)
            
            # 合并所有关键点数据
            landmarks_data = {
                'pose': [],
                'face': [],
                'left_hand': [],
                'right_hand': []
            }
            
            # 添加姿态关键点
            if pose_results.pose_landmarks:
                for landmark in pose_results.pose_landmarks.landmark:
                    landmarks_data['pose'].append({
                        'x': landmark.x,
                        'y': landmark.y,
                        'z': landmark.z,
                        'visibility': landmark.visibility
                    })
            
            # 添加面部关键点
            if face_results.multi_face_landmarks:
                for landmark in face_results.multi_face_landmarks[0].landmark:
                    landmarks_data['face'].append({
                        'x': landmark.x,
                        'y': landmark.y,
                        'z': landmark.z
                    })
            
            # 添加手部关键点
            if hands_results.multi_hand_landmarks:
                for hand_idx, hand_landmarks in enumerate(hands_results.multi_hand_landmarks):
                    # 确定是左手还是右手
                    handedness = hands_results.multi_handedness[hand_idx].classification[0].label
                    hand_type = 'left_hand' if handedness == 'Left' else 'right_hand'
                    
                    for landmark in hand_landmarks.landmark:
                        landmarks_data[hand_type].append({
                            'x': landmark.x,
                            'y': landmark.y,
                            'z': landmark.z
                        })
            
            # 发送所有关键点数据
            if any(landmarks_data.values()):
                socketio.emit('pose_data', landmarks_data)
                logger.info(f"发送关键点数据: 姿态={len(landmarks_data['pose'])}, "
                          f"面部={len(landmarks_data['face'])}, "
                          f"左手={len(landmarks_data['left_hand'])}, "
                          f"右手={len(landmarks_data['right_hand'])} 个关键点")
            
        except Exception as e:
            logger.error(f"处理关键点时出错: {str(e)}")
            continue
            
        # 转换帧格式用于传输
        try:
            ret, buffer = cv2.imencode('.jpg', frame)  # 直接使用原始帧
            if not ret:
                continue
            frame_bytes = buffer.tobytes()
            yield (b'--frame\r\n'
                   b'Content-Type: image/jpeg\r\n\r\n' + frame_bytes + b'\r\n')
        except Exception as e:
            logger.error(f"编码帧时出错: {str(e)}")

@app.route('/camera_status')
def camera_status():
    """获取摄像头状态"""
    try:
        status = {
            "isRunning": camera_manager.is_running,
            "fps": camera_manager.current_fps,
            "status": "running" if camera_manager.is_running else "stopped"
        }
        return jsonify(status)
    except Exception as e:
        logger.error(f"获取摄像头状态失败: {str(e)}")
        return jsonify({"error": str(e)}), 500

@socketio.on('connect')
def handle_connect():
    """处理客户端连接"""
    logger.info("客户端已连接")
    pose_sender.connect(socketio)

@socketio.on('disconnect')
def handle_disconnect():
    """处理客户端断开连接"""
    logger.info("客户端已断开")
    pose_sender.disconnect()

@app.route('/api/upload_audio', methods=['POST'])
def upload_audio():
    """上传音频文件"""
    try:
        if 'audio' not in request.files:
            return jsonify({
                'status': 'error',
                'message': '没有上传文件'
            }), 400
            
        file = request.files['audio']
        if file.filename == '':
            return jsonify({
                'status': 'error', 
                'message': '未选择文件'
            }), 400
            
        # 确保上传目录存在
        audio_dir = os.path.join(UPLOAD_FOLDER, 'audio')
        os.makedirs(audio_dir, exist_ok=True)
        
        # 保存文件
        filename = secure_filename(file.filename)
        file_path = os.path.join(audio_dir, filename)
        file.save(file_path)
        
        return jsonify({
            'status': 'success',
            'message': '音频上传成功',
            'audio_url': os.path.join('/uploads/audio', filename)
        })
        
    except Exception as e:
        return jsonify({
            'status': 'error',
            'message': str(e)
        }), 500

@app.route('/audio/<filename>')
def stream_audio(filename):
    """流式传输音频文件"""
    def generate():
        audio_path = os.path.join(UPLOAD_FOLDER, 'audio', filename)
        with open(audio_path, 'rb') as audio_file:
            data = audio_file.read(1024)
            while data:
                yield data
                data = audio_file.read(1024)
                
    return Response(generate(), mimetype='audio/mpeg')

@app.errorhandler(Exception)
def handle_error(error):
    """全局错误处理"""
    logger.error(f"发生错误: {str(error)}")
    return jsonify({
        'success': False,
        'error': str(error)
    }), 500

@app.route('/camera/settings', methods=['GET', 'POST'])
def camera_settings():
    """获取或更新相机设置"""
    if request.method == 'GET':
        return jsonify(camera_manager.get_settings())
        
    settings = request.json
    success = camera_manager.update_settings(settings)
    return jsonify({'success': success})

@app.route('/camera/reset', methods=['POST'])
def reset_camera():
    """重置相机设置"""
    success = camera_manager.reset_settings()
    return jsonify({'success': success})

@app.route('/status')
def get_status():
    """获取当前状态"""
    try:
        status = {
            'camera': {
                'isActive': camera_manager.is_running,
                'fps': camera_manager.current_fps
            },
            'room': {
                'isConnected': socket_manager.is_connected,
                'roomId': socket_manager.current_room
            }
        }
        return jsonify(status)
    except Exception as e:
        logger.error(f"获取状态失败: {str(e)}")
        return jsonify({'error': str(e)}), 500

@app.route('/verify_identity', methods=['POST'])
def verify_identity():
    """验证当前人脸与参考帧是否匹配"""
    try:
        # 检查是否有参考帧
        reference_path = os.path.join(project_root, 'output', 'reference.jpg')
        if not os.path.exists(reference_path):
            return jsonify({
                'success': False,
                'message': '请先捕获参考帧'
            })
            
        # 获取当前帧
        success, current_frame = camera_manager.read()
        if not success:
            return jsonify({
                'success': False,
                'message': '无法获取当前画面'
            })
            
        # 读取参考帧
        reference_frame = cv2.imread(reference_path)
        
        # 进行人脸验证
        verifier = FaceVerifier()
        if verifier.set_reference(reference_frame):
            result = verifier.verify_face(current_frame)
            
            return jsonify({
                'success': True,
                'verification': {
                    'passed': result.is_same_person,
                    'confidence': float(result.confidence),
                    'message': result.error_message
                }
            })
            
        return jsonify({
            'success': False,
            'message': '人脸验证初始化失败'
        })
        
    except Exception as e:
        logger.error(f"身份验证错误: {str(e)}")
        return jsonify({
            'success': False,
            'message': f'错误: {str(e)}'
        })

def init_pose_system():
    """初始化姿态处理系统"""
    try:
        # 初始化姿态检测器
        logger.info("正在初始化姿态检测器...")
        pose_detector = PoseDetector()
        
        # 初始化姿态绑定器
        logger.info("正在初始化姿态绑定器...")
        pose_binding = PoseBinding()
        
        # 初始化绘制器
        logger.info("正在初始化姿态绘制器...")
        pose_drawer = PoseDrawer()
        
        return pose_detector, pose_binding, pose_drawer
        
    except Exception as e:
        logger.error(f"姿态系统初始化失败: {str(e)}")
        raise

async def setup_jitsi():
    # transport = JitsiTransport(JITSI_CONFIG)
    # meeting_manager = JitsiMeetingManager(JITSI_CONFIG)
    
    return None, None

async def main():
    # ... 其他代码 ...
    
    # 注释掉 Jitsi 相关的初始化和设置
    '''
    # 初始化 Jitsi 会议管理器
    meeting_manager = JitsiMeetingManager(JITSI_CONFIG)
    await meeting_manager.start()
    
    try:
        default_room_id = "default_room"
        host_id = "host_1"
        room_id = await meeting_manager.create_meeting(
            room_id=default_room_id,
            host_id=host_id
        )
        logger.info(f"Created default meeting room: {room_id}")
    except Exception as e:
        logger.error(f"Failed to create default meeting room: {e}")
        raise
    '''
    
    try:
        # 直接使用 Flask 的 run 方法
        app.run(
            host='0.0.0.0',
            port=5000,
            debug=True  # 开发模式
        )
    except Exception as e:
        logger.error(f"Failed to start web server: {e}")
        raise
    finally:
        pass
        # await meeting_manager.stop()  # 注释掉

if __name__ == "__main__":
    # 配置日志
    logging.basicConfig(level=logging.INFO)
    
    # 抑制 TensorFlow 和 Mediapipe 警告
    os.environ['TF_CPP_MIN_LOG_LEVEL'] = '2'
    logging.getLogger('tensorflow').setLevel(logging.ERROR)
    absl.logging.set_verbosity(absl.logging.ERROR)
    logging.getLogger('mediapipe').setLevel(logging.ERROR)
    
    try:
        # 创建必要的目录
        os.makedirs(UPLOAD_FOLDER, exist_ok=True)
        
        # 运行主程序
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n程序被用户中断")
    except Exception as e:
        print(f"程序出错: {e}")
        logger.exception("程序异常退出")
    finally:
        # 清理资源
        cv2.destroyAllWindows()