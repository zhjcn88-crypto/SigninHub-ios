//
//  main.m — SigninHub iOS 引导
//
//  移植自 beeware/briefcase-iOS-Xcode-template（CPython 官方 iOS 嵌入范式）。
//  职责：
//    1. 起嵌入式 CPython 3.13，PYTHONPATH 指到 bundle 里的 stdlib / lib-dynload / app；
//    2. 在独立线程里调用 signbot.server.serve(host="127.0.0.1", port=8787)；
//    3. 立即返回，把事件循环交给 UIKit，WebView 由 Swift 侧加载本机页面。
//
//  线程模型：Python 服务线程长期持有解释器；UI 线程只做 WebView。
//  这里刻意不用 PyEval_SaveThread 把 GIL 交回主线程——主线程根本不碰 Python，
//  避免 GIL 与 UIKit 卡周期互相拖累。
//

#import <Foundation/Foundation.h>
#import <UIKit/UIKit.h>
#import <Python/Python.h>
#import <pthread.h>
#import <string.h>
#import <unistd.h>     // sleep()：服务崩溃后的退避重启要用

static char gErrorBuffer[8192];

/// 把当前 Python 异常渲染成可读文本（含 traceback）。
static void SigninHubCaptureError(const char *context) {
    PyObject *type = NULL, *value = NULL, *tb = NULL;
    PyErr_Fetch(&type, &value, &tb);
    PyErr_NormalizeException(&type, &value, &tb);

    NSMutableString *out = [NSMutableString string];
    if (context) {
        [out appendFormat:@"%s\n", context];
    }
    if (value != NULL) {
        PyObject *s = PyObject_Str(value);
        if (s != NULL) {
            const char *c = PyUnicode_AsUTF8(s);
            if (c != NULL) {
                [out appendFormat:@"%s\n", c];
            }
            Py_DECREF(s);
        }
    }
    if (tb != NULL) {
        PyObject *tbmod = PyImport_ImportModule("traceback");
        if (tbmod != NULL) {
            PyObject *fmt = PyObject_GetAttrString(tbmod, "format_exception");
            if (fmt != NULL) {
                PyObject *lines = PyObject_CallFunctionObjArgs(fmt, type, value, tb, NULL);
                if (lines != NULL) {
                    PyObject *sep = PyUnicode_FromString("");
                    PyObject *joined = PyUnicode_Join(sep, lines);
                    if (joined != NULL) {
                        const char *c = PyUnicode_AsUTF8(joined);
                        if (c != NULL) {
                            [out appendFormat:@"%s", c];
                        }
                        Py_DECREF(joined);
                    }
                    Py_XDECREF(sep);
                    Py_DECREF(lines);
                }
                Py_DECREF(fmt);
            }
            Py_DECREF(tbmod);
        }
    }
    Py_XDECREF(type);
    Py_XDECREF(value);
    Py_XDECREF(tb);

    strncpy(gErrorBuffer, [out UTF8String] ?: "unknown", sizeof(gErrorBuffer) - 1);
    gErrorBuffer[sizeof(gErrorBuffer) - 1] = '\0';
    NSLog(@"[SigninHub] %@", out);
}

/// 初始化嵌入式解释器。
static int SigninHubStartPython(void) {
    PyStatus status;
    PyPreConfig preconfig;
    PyConfig config;
    wchar_t *wide = NULL;

    NSString *resourcePath = [[NSBundle mainBundle] resourcePath];
    NSString *pythonHome = [NSString stringWithFormat:@"%@/python", resourcePath];

    setenv("LANG", "en_US.UTF-8", 1);

    PyPreConfig_InitIsolatedConfig(&preconfig);
    PyConfig_InitIsolatedConfig(&config);

    preconfig.utf8_mode = 1;
    preconfig.configure_locale = 1;
    config.buffered_stdio = 0;
    config.write_bytecode = 0;          // 包体只读
    config.install_signal_handlers = 1;
    config.module_search_paths_set = 1;
    config.parse_argv = 0;
    // 注意：不要设置 config.use_system_logger —— 那是 Python 3.14 才有的字段，
    // 3.13 的 PyConfig 里不存在，写了会直接编译失败。
    // 3.13 在 iOS 上默认就把 stdout/stderr 写进系统日志（NSLog 可见），正合需要。

    status = Py_PreInitialize(&preconfig);
    if (PyStatus_Exception(status)) {
        SigninHubCaptureError("Py_PreInitialize 失败");
        PyConfig_Clear(&config);
        return -1;
    }

    wide = Py_DecodeLocale([pythonHome UTF8String], NULL);
    status = PyConfig_SetString(&config, &config.home, wide);
    PyMem_RawFree(wide);
    if (PyStatus_Exception(status)) {
        SigninHubCaptureError("设置 PYTHONHOME 失败");
        PyConfig_Clear(&config);
        return -1;
    }

    status = PyConfig_Read(&config);
    if (PyStatus_Exception(status)) {
        SigninHubCaptureError("PyConfig_Read 失败");
        PyConfig_Clear(&config);
        return -1;
    }

    NSArray<NSString *> *searchPaths = @[
        [NSString stringWithFormat:@"%@/lib/python3.13", pythonHome],
        [NSString stringWithFormat:@"%@/lib/python3.13/lib-dynload", pythonHome],
        [NSString stringWithFormat:@"%@/app", resourcePath],
        [NSString stringWithFormat:@"%@/app_packages", resourcePath],
    ];
    for (NSString *p in searchPaths) {
        wide = Py_DecodeLocale([p UTF8String], NULL);
        status = PyWideStringList_Append(&config.module_search_paths, wide);
        PyMem_RawFree(wide);
        if (PyStatus_Exception(status)) {
            SigninHubCaptureError("设置模块搜索路径失败");
            PyConfig_Clear(&config);
            return -1;
        }
    }

    status = Py_InitializeFromConfig(&config);
    PyConfig_Clear(&config);
    if (PyStatus_Exception(status)) {
        SigninHubCaptureError("Py_InitializeFromConfig 失败");
        return -1;
    }
    return 0;
}

/// 调用 signbot.server.serve()（阻塞）。
static int SigninHubRunServer(void) {
    // 必须先跑引导：修 CA 证书、把数据目录钉到沙箱、设临时目录。
    // 顺序很关键——config.py 在 import 期就会读 SIGNBOT_DATA 并 mkdir。
    PyObject *boot = PyImport_ImportModule("_ios_boot");
    if (boot == NULL) {
        SigninHubCaptureError("导入 _ios_boot 失败（app/ 是否完整？）");
        return -1;
    }

    // prepare_server()：清掉老口令 + 关掉 serve() 的「自动生成口令」。
    // 不做这步，serve() 会生成随机口令写盘，而 WebView 拿不到它，
    // 所有 /api/* 返回 401，页面直接不可用。
    PyObject *prep = PyObject_GetAttrString(boot, "prepare_server");
    if (prep != NULL && PyCallable_Check(prep)) {
        PyObject *r = PyObject_CallObject(prep, NULL);
        if (r == NULL) {
            SigninHubCaptureError("prepare_server() 失败");
            Py_DECREF(prep);
            Py_DECREF(boot);
            return -1;
        }
        Py_DECREF(r);
    }
    Py_XDECREF(prep);
    Py_DECREF(boot);

    PyObject *mod = PyImport_ImportModule("signbot.server");
    if (mod == NULL) {
        SigninHubCaptureError("导入 signbot.server 失败");
        return -1;
    }
    PyObject *serveFn = PyObject_GetAttrString(mod, "serve");
    if (serveFn == NULL || !PyCallable_Check(serveFn)) {
        Py_XDECREF(serveFn);
        Py_DECREF(mod);
        SigninHubCaptureError("signbot.server.serve 不可调用");
        return -1;
    }

    PyObject *host = PyUnicode_FromString("127.0.0.1");
    PyObject *port = PyLong_FromLong(8787);
    PyObject *args = PyTuple_Pack(2, host, port);
    Py_XDECREF(host);
    Py_XDECREF(port);

    PyObject *ret = PyObject_CallObject(serveFn, args);
    Py_XDECREF(args);
    Py_DECREF(serveFn);
    Py_DECREF(mod);

    if (ret == NULL) {
        SigninHubCaptureError("serve() 抛出异常");
        return -1;
    }
    Py_DECREF(ret);
    return 0;
}

static void *SigninHubPythonMain(void *unused) {
    @autoreleasepool {
        if (SigninHubStartPython() != 0) {
            return NULL;
        }

        // 关键：初始化完成后立刻释放 GIL，让其他线程（BGTaskScheduler 唤醒时
        // 调用的 SHBRunCheckin）能通过 PyGILState_Ensure 拿到锁。
        // 若不释放，本线程会一直持有 GIL，后台唤醒时 Ensure 会永久阻塞。
        // 后续本线程自己的调用都要重新 Ensure/Release。
        PyThreadState *tstate = PyEval_SaveThread();

        // serve_forever() 正常不会返回；一旦返回或抛异常（端口被占、
        // 磁盘满、上游数据损坏……），App 就永久白屏 —— 用户只能杀进程重开。
        // 这里做有界重试：退避后重启服务，超过上限才放弃。
        const int kMaxRestarts = 5;
        int attempt = 0;
        while (attempt <= kMaxRestarts) {
            PyGILState_STATE gil = PyGILState_Ensure();
            int rc = SigninHubRunServer();     // 阻塞（serve_forever）
            PyGILState_Release(gil);

            if (rc == 0) {
                // 正常退出（理论上不会发生）
                NSLog(@"[SigninHub] serve() 正常返回，退出服务线程");
                break;
            }

            attempt++;
            if (attempt > kMaxRestarts) {
                NSLog(@"[SigninHub] 服务连续失败 %d 次，放弃重启", attempt - 1);
                break;
            }
            // 1s, 2s, 4s, 8s, 16s 退避
            int delay = 1 << (attempt - 1);
            NSLog(@"[SigninHub] 服务异常退出，%d 秒后第 %d 次重启", delay, attempt);
            sleep((unsigned int)delay);
        }

        PyEval_RestoreThread(tstate);
        Py_Finalize();
    }
    return NULL;
}

int main(int argc, char *argv[]) {
    @autoreleasepool {
        // 先起 Python 服务线程，再让 UIKit 接管主线程。
        pthread_t tid;
        pthread_attr_t attr;
        pthread_attr_init(&attr);
        pthread_attr_setdetachstate(&attr, PTHREAD_CREATE_DETACHED);
        pthread_create(&tid, &attr, SigninHubPythonMain, NULL);
        pthread_attr_destroy(&attr);
    }
    return UIApplicationMain(argc, argv, nil, @"SigninHubAppDelegate");
}
