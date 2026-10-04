# -*- coding: utf-8 -*-
"""autopilot —— Windows 全自动电脑操控框架。

分层：
    winapi / screen / windows  底层 Win32 能力（纯 ctypes，零第三方依赖）
    psbridge / uia / ocr       感知层：UI Automation 元素树 + 系统 OCR
    perceive                   把感知结果组装成可读文本，供人和 LLM 消费
    registry / actions         动作注册表（模型可调用的全部能力）
    executor / guard           执行、审计与安全护栏
    llm / agent                DeepSeek 驱动的自主决策循环
    flows                      确定性任务脚本（不依赖 LLM 的回放）
    server                     Web 控制台
"""

__version__ = "1.0.0"
__all__ = ["__version__"]
