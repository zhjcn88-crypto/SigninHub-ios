//
//  SigninHub-Bridging-Header.h
//
//  Swift 侧只能看到这里声明的 C 函数。Python C API 全部封装在 PythonBridge.m，
//  Swift 不直接 include Python.h（会与 Swift 的宏/命名冲突）。
//

#import <Foundation/Foundation.h>

/// 执行一次签到。trigger 记进签到记录，返回 0 表示调用成功。
int SHBRunCheckin(const char *trigger);

/// Python 解释器是否已就绪。
int SHBPythonReady(void);
