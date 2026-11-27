//
//  PythonBridge.m — Swift 调 Python 的 C 桥
//
//  为什么单独一个文件：
//    Swift 不能直接 include Python.h 并调 C API（会撞上宏与命名冲突），
//    标准做法是写一层薄薄的 ObjC 函数，再经 bridging header 暴露给 Swift。
//
//  线程：后台任务可能在任意线程唤醒。解释器由 Python 服务线程创建并持有，
//  这里用 PyGILState_Ensure/Release 安全地拿 GIL。
//

#import <Foundation/Foundation.h>
#import <Python/Python.h>

/// 执行一次签到。trigger 会记进签到记录（"schedule" / "background" / "manual"）。
/// 返回 0 表示调用成功（不代表签到一定成功，具体结果看记录）。
int SHBRunCheckin(const char *trigger) {
    if (trigger == NULL) {
        trigger = "background";
    }

    PyGILState_STATE gil = PyGILState_Ensure();

    int rc = -1;
    PyObject *mod = PyImport_ImportModule("signbot.runner");
    if (mod != NULL) {
        PyObject *fn = PyObject_GetAttrString(mod, "run_all");
        if (fn != NULL && PyCallable_Check(fn)) {
            // run_all(trigger="...") 全用关键字参数，位置参数给空元组。
            PyObject *args = PyTuple_New(0);
            PyObject *kwargs = PyDict_New();
            PyObject *arg = PyUnicode_FromString(trigger);
            PyObject *res = NULL;

            if (args != NULL && kwargs != NULL && arg != NULL
                && PyDict_SetItemString(kwargs, "trigger", arg) == 0) {
                res = PyObject_Call(fn, args, kwargs);
            }

            if (res != NULL) {
                rc = 0;
                Py_DECREF(res);
            } else {
                PyErr_Print();      // 打到系统日志，Xcode 里能看到 traceback
            }

            Py_XDECREF(arg);
            Py_XDECREF(kwargs);
            Py_XDECREF(args);
            Py_DECREF(fn);
        } else {
            PyErr_SetString(PyExc_RuntimeError, "signbot.runner.run_all 不可调用");
            PyErr_Print();
            Py_XDECREF(fn);
        }
        Py_DECREF(mod);
    } else {
        PyErr_Print();
    }

    PyGILState_Release(gil);
    return rc;
}

/// 返回 Python 是否已就绪（服务是否已经跑起来）。供 UI 侧查状态用。
int SHBPythonReady(void) {
    if (!Py_IsInitialized()) {
        return 0;
    }
    PyGILState_STATE gil = PyGILState_Ensure();
    PyObject *srv = PyImport_ImportModule("signbot.server");
    int ready = (srv != NULL);
    Py_XDECREF(srv);
    PyGILState_Release(gil);
    return ready;
}
