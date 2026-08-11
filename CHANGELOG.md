# 更新日志

## [v1.0.1] - 2026-08-11

### 新增
- 添加插件 logo

### 变更
- 显示名称由「3D 模型工厂」改为「3D模型生成器」
- 插件名由 `astrbot_plugin_3dmodel` 改为 `astrbot_plugin_JO3dmodel`
- 作者由 `WorkBuddy` 改为 `Jonathan`

### 修复
- 修复 `main.py` 中绝对导入导致插件加载失败的问题（`from core.xxx` → `from .core.xxx`）

---

## [v1.0.0] - 2026-08-11

### 新增
- 首个版本发布
- 本地程序化生成 3D 模型（球体、花瓶、齿轮、地形、文字浮雕等 15+ 种）
- AI 文生 3D 支持（Tripo / Meshy / 腾讯混元）
- 输出 STL / OBJ / GLB 格式 + PNG 预览图 + HTML 交互预览
- 支持 AstrBot v4 插件规范