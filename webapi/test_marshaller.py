"""验证 QtMarshaller 两种调用场景都不死锁/不超时。"""
import sys, threading
sys.path.insert(0, '.')
from PySide6.QtWidgets import QApplication
from PySide6.QtCore import QTimer
from webapi.radio_bridge import marshaller

app = QApplication(sys.argv)
marshaller.start()
QT = threading.current_thread()
res = {}

def _from_worker():                       # 场景 A：非 Qt 线程（真实：FastAPI 线程池）
    try:
        res['A_worker'] = marshaller.call(lambda: 42, timeout=4)
    except Exception as e:
        res['A_worker'] = f'ERR {type(e).__name__}'

def _from_qt():                           # 场景 B：Qt 线程（有保护 → 直接执行）
    try:
        res['B_qt'] = marshaller.call(lambda: 7, timeout=4)
    except Exception as e:
        res['B_qt'] = f'ERR {type(e).__name__}'

def _kick():
    t = threading.Thread(target=_from_worker, daemon=True); t.start()
    def _after():
        _from_qt()
        app.quit()
    QTimer.singleShot(700, _after)        # 等工作线程完成再测 Qt 线程

QTimer.singleShot(200, _kick)
QTimer.singleShot(9000, app.quit)
app.exec()

a_ok = res.get('A_worker') == 42
b_ok = res.get('B_qt') == 7
print('A 非Qt线程调用(真实场景):', res.get('A_worker'), '->', 'OK' if a_ok else 'FAIL')
print('B Qt线程调用(死锁保护)  :', res.get('B_qt'),    '->', 'OK' if b_ok else 'FAIL')
print()
print('MARSHAL_BOTH_OK' if (a_ok and b_ok) else 'MARSHAL_FAIL')
