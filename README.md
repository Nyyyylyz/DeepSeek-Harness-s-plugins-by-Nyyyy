# DeepSeek-Harness plugins by Nyyyy

一堆自己用着顺手的小插件。每个插件一个子目录，装法见各自 README。

## 插件列表

| 插件 | 说明 | 状态 |
|---|---|---|
| [solidworks](./solidworks) | 在对话里驱动本机 SolidWorks 建模：给三视图，出实体模型 + 正交视图 + 第一角三视图工程图 | 0.1.0 |

## 安装

每个子目录都是一个独立的 DSH bundle，装法二选一：

1. **插件页面**：Plugins → Add plugin → 选择对应子目录；
2. **命令行**：让 agent 执行 `plugin_manager install_bundle`，target 指向该子目录的绝对路径。

## 许可

各插件自带 LICENSE，默认 MIT。