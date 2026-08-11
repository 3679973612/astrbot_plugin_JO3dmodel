"""AstrBot 插件：3D 模型工厂。

在聊天中直接创建 3D 模型：
- 本地程序化生成：/3d 花瓶 高度=10 样式=tulip（无需联网、无需 API Key）
- AI 文生 3D：/3d ai 一只机械猫（需在插件配置中填写 Tripo / Meshy API Key）
- 输出：STL / OBJ / GLB 模型文件 + PNG 预览图 + 可旋转的 HTML 交互预览

完美适配 AstrBot v4：
- 符合插件规范（metadata.yaml + Star + @register + _conf_schema.json）
- 异步实现：CPU 密集操作放入线程池（asyncio.to_thread），不阻塞事件循环
- 使用 aiohttp 异步网络请求（遵循 AstrBot 规范，不使用 requests）
- 数据存 AstrBot 数据目录，错误处理完善，全中文提示
- 提供 LLM 工具 create_3d_model，AI 对话中可自动调用生成模型
"""

from __future__ import annotations

import asyncio
import os
import time

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import File, Image, Plain
from astrbot.api.star import Context, Star, register

from .core.ai_generator import AI3DError, AI3DGenerator
from .core.generators import generate, list_models, resolve_name
from .core.mesh import Mesh
from .core.preview import generate_html_preview
from .core.renderer import render_to_png
from .core.utils import format_model_help, parse_params, safe_filename

#: 指令别名（中英文）
CMD_HEADERS = ("3d", "3dmodel", "3d模型", "造模型", "做3d", "3d打印")

HELP_TEXT = (
    "🧊 3D 模型工厂使用说明\n"
    "━━━━━━━━━━━━━━━━\n"
    "① 本地生成：`/3d <模型> [参数]`\n"
    "   例：/3d 花瓶 高度=10 样式=tulip\n"
    "       /3d gear teeth 16 radius 2.2\n"
    "② AI 生成：`/3d ai <描述>`（需配置 API Key）\n"
    "   例：/3d ai 一只戴帽子的机械猫\n"
    "③ 模型清单：`/3d list`\n"
    "④ 本帮助：`/3d help`\n"
    "━━━━━━━━━━━━━━━━\n"
    "输出：模型文件（STL/OBJ/GLB）+ PNG 预览图 + HTML 交互预览"
)


@register(
    "astrbot_plugin_JO3dmodel",
    "Jonathan",
    "在聊天中创建 3D 模型：本地程序化生成（球体/花瓶/齿轮/地形/文字浮雕等 15+ 种）+ AI 文生 3D（Tripo/Meshy），输出 STL/OBJ/GLB 文件与预览图",
    "v1.2.0",
)
class ThreeDModelPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        # 输出目录：优先使用 AstrBot 注入的数据目录（重装/更新不丢失）
        data_dir = getattr(self, "data_dir", None) or getattr(self.context, "data_dir", None)
        if not data_dir:
            data_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
        self.output_dir = os.path.join(str(data_dir), "3dmodel")
        os.makedirs(self.output_dir, exist_ok=True)
        logger.info(f"[3DModel] 插件已加载，输出目录: {self.output_dir}")

    async def initialize(self):
        """插件初始化：确保输出目录存在。"""
        os.makedirs(self.output_dir, exist_ok=True)

    async def terminate(self):
        """插件卸载时的清理钩子。"""
        logger.info("[3DModel] 插件已卸载")

    # ------------------------------------------------------------------ #
    # 主指令
    # ------------------------------------------------------------------ #
    @filter.command("3d", alias={"3dmodel", "3d模型", "造模型", "做3d", "3d打印"})
    async def cmd_3d(self, event: AstrMessageEvent):
        """创建 3D 模型：/3d <模型> [参数] 或 /3d ai <描述> 或 /3d list / help"""
        args = self._strip_cmd_prefix(event.message_str).strip()
        if not args:
            yield event.plain_result(HELP_TEXT)
            return

        # 子命令分发
        head, _, rest = args.partition(" ")
        head = head.strip().lower()

        if head in ("help", "帮助", "-h", "--help"):
            yield event.plain_result(HELP_TEXT)
            return
        if head in ("list", "列表", "清单", "models"):
            yield event.plain_result(format_model_help())
            return
        if head in ("ai", "ai生成", "智能"):
            if not rest.strip():
                yield event.plain_result("用法：/3d ai <描述>\n例如：/3d ai 一只戴帽子的机械猫")
                return
            await self._handle_ai(event, rest.strip())
            return

        # 本地生成
        await self._handle_local(event, args)

    @staticmethod
    def _strip_cmd_prefix(message: str) -> str:
        """去掉消息中的指令前缀（含斜杠与别名）。"""
        msg = (message or "").strip()
        for header in CMD_HEADERS:
            for prefix in (f"/{header}", f"{header}"):
                if msg.lower().startswith(prefix.lower()):
                    return msg[len(prefix):]
        return msg

    # ------------------------------------------------------------------ #
    # 本地程序化生成
    # ------------------------------------------------------------------ #
    async def _handle_local(self, event: AstrMessageEvent, args: str):
        """本地生成流程。"""
        model_name, kwargs = parse_params(args)
        if not model_name:
            yield event.plain_result("请指定模型类型，例如：/3d 花瓶\n查看全部：/3d list")
            return
        resolved = resolve_name(model_name)
        if resolved is None:
            yield event.plain_result(
                f"❌ 未知模型「{model_name}」。\n可用模型：{', '.join(list_models())}\n查看详细说明：/3d list"
            )
            return

        await event.send(f"⏳ 正在生成「{model_name}」...")
        try:
            mesh, file_path, png_path, html_path, out_fmt = await asyncio.to_thread(
                self._build_local, resolved, kwargs
            )
        except Exception as exc:  # noqa: BLE001
            logger.error(f"[3DModel] 本地生成失败: {exc}")
            yield event.plain_result(f"❌ 生成失败：{exc}\n可尝试 /3d help 查看用法")
            return

        size_kb = os.path.getsize(file_path) / 1024
        param_str = " ".join(f"{k}={v}" for k, v in kwargs.items()) or "默认"
        msg = (
            f"🧊 已生成【{resolved}】模型！\n"
            f"· 顶点 {mesh.vertex_count:,} | 三角面 {mesh.face_count:,}\n"
            f"· 格式 {out_fmt.upper()} | 大小 {size_kb:.1f} KB\n"
            f"· 参数：{param_str}\n"
            "· 可直接用于 3D 打印 / Blender / 游戏引擎"
        )
        yield self._make_result(event, msg, file_path, png_path, html_path, out_fmt)

    def _build_local(self, resolved: str, kwargs: dict):
        """子线程执行：生成 + 导出 + 渲染。"""
        max_tri = int(self.config.get("max_triangle_count", 60000))
        fmt = str(self.config.get("default_format", "stl")).lower()
        mesh = generate(resolved, max_triangles=max_tri, **kwargs)
        return self._finalize(mesh, resolved, fmt)

    # ------------------------------------------------------------------ #
    # AI 文生 3D
    # ------------------------------------------------------------------ #
    async def _handle_ai(self, event: AstrMessageEvent, prompt: str):
        """AI 生成流程。"""
        provider = str(self.config.get("ai_provider", "tripo")).lower()
        api_key = str(self.config.get(f"{provider}_api_key", "")).strip()
        model_cfg = str(self.config.get("ai_model", "")).strip()
        out_fmt = str(self.config.get("default_format", "stl")).lower()

        # 腾讯混元：cloud 模式用 SecretId/Key，local 模式用本地服务地址
        if provider == "hunyuan":
            hunyuan_mode = str(self.config.get("hunyuan_mode", "cloud")).lower()
            if hunyuan_mode == "local":
                if not str(self.config.get("hunyuan_local_url", "")).strip():
                    yield event.plain_result(
                        "⚠️ 本地混元模式需要先配置「hunyuan_local_url」\n"
                        "（部署指南见 README：git clone Tencent-Hunyuan/Hunyuan3D-2 && python api_server.py --port 8080）"
                    )
                    return
            else:
                if not (str(self.config.get("hunyuan_secret_id", "")).strip()
                        and str(self.config.get("hunyuan_secret_key", "")).strip()):
                    yield event.plain_result(
                        "⚠️ 腾讯混元（云 API）需要先配置：\n"
                        "· hunyuan_secret_id（SecretId）\n"
                        "· hunyuan_secret_key（SecretKey）\n"
                        "开通地址：cloud.tencent.com/product/1804（有免费额度）\n"
                        "或改用本地部署：hunyuan_mode=local"
                    )
                    return
            await event.send(f"⏳ 腾讯混元正在建模「{prompt[:40]}」...（通常需 1~5 分钟）")
            try:
                mesh, file_path, png_path, html_path, out_fmt = await self._build_hunyuan(
                    prompt, hunyuan_mode, out_fmt
                )
            except AI3DError as exc:
                yield event.plain_result(f"❌ {exc}")
                return
            except Exception as exc:  # noqa: BLE001
                logger.error(f"[3DModel] 混元生成失败: {exc}")
                yield event.plain_result(f"❌ 混元生成失败：{exc}")
                return
            size_kb = os.path.getsize(file_path) / 1024
            mode_name = "腾讯云 API" if hunyuan_mode == "cloud" else "本地部署开源模型"
            msg = (
                f"🤖 混元3D 建模完成！\n"
                f"· 描述：{prompt}\n"
                f"· 顶点 {mesh.vertex_count:,} | 三角面 {mesh.face_count:,}\n"
                f"· 格式 {out_fmt.upper()} | 大小 {size_kb:.1f} KB\n"
                f"· 接入方式：{mode_name}"
            )
            yield self._make_result(event, msg, file_path, png_path, html_path, out_fmt)
            return

        if not api_key:
            site = "platform.tripo3d.ai" if provider == "tripo" else "www.meshy.ai"
            yield event.plain_result(
                f"⚠️ AI 文生 3D 需要先配置 API Key：\n"
                f"在插件配置中填写「{provider}_api_key」（{site} 注册）\n"
                f"或改用本地生成：/3d 花瓶"
            )
            return

        await event.send(f"⏳ AI 正在建模「{prompt[:40]}」...（{provider} 通常需 1~5 分钟）")
        try:
            mesh, file_path, png_path, html_path, out_fmt = await self._build_ai(
                prompt, provider, api_key, model_cfg, out_fmt
            )
        except AI3DError as exc:
            yield event.plain_result(f"❌ {exc}")
            return
        except Exception as exc:  # noqa: BLE001
            logger.error(f"[3DModel] AI 生成失败: {exc}")
            yield event.plain_result(f"❌ AI 生成失败：{exc}")
            return

        size_kb = os.path.getsize(file_path) / 1024
        msg = (
            f"🤖 AI 建模完成！\n"
            f"· 描述：{prompt}\n"
            f"· 顶点 {mesh.vertex_count:,} | 三角面 {mesh.face_count:,}\n"
            f"· 格式 {out_fmt.upper()} | 大小 {size_kb:.1f} KB\n"
            f"· 服务商：{provider}"
        )
        yield self._make_result(event, msg, file_path, png_path, html_path, out_fmt)

    async def _build_ai(self, prompt: str, provider: str, api_key: str,
                        model_cfg: str, out_fmt: str):
        """异步执行 AI 生成（提交任务 + 轮询等待）。"""
        gen = AI3DGenerator(provider=provider, api_key=api_key, model=model_cfg)
        dl_formats = ["glb", "obj"]
        if out_fmt not in dl_formats:
            dl_formats.append(out_fmt)
        mesh, files = await gen.generate(prompt, self.output_dir, download_formats=dl_formats)

        # 归一化后导出目标格式
        mesh.recenter().normalize_scale(target=2.0)
        return self._finalize(mesh, f"ai_{safe_filename(prompt)}", out_fmt)

    async def _build_hunyuan(self, prompt: str, mode: str, out_fmt: str):
        """异步执行腾讯混元生成（云 API 或本地部署）。"""
        cfg = self.config
        gen = AI3DGenerator(
            provider="hunyuan",
            hunyuan_mode=mode,
            hunyuan_secret_id=str(cfg.get("hunyuan_secret_id", "")),
            hunyuan_secret_key=str(cfg.get("hunyuan_secret_key", "")),
            hunyuan_local_url=str(cfg.get("hunyuan_local_url", "http://127.0.0.1:8080")),
            hunyuan_result_format=str(cfg.get("hunyuan_result_format", "GLB")),
            hunyuan_enable_pbr=bool(cfg.get("hunyuan_enable_pbr", False)),
            hunyuan_use_pro=bool(cfg.get("hunyuan_use_pro", False)),
        )
        mesh, files = await gen.generate(prompt, self.output_dir)

        # 归一化后导出目标格式
        mesh.recenter().normalize_scale(target=2.0)
        return self._finalize(mesh, f"ai_{safe_filename(prompt)}", out_fmt)

    # ------------------------------------------------------------------ #
    # 公共：导出 + 渲染 + 组装回复
    # ------------------------------------------------------------------ #
    def _finalize(self, mesh: Mesh, tag: str, fmt: str):
        """导出模型 + 渲染 PNG + 生成 HTML 预览，返回各文件路径。"""
        ts = int(time.time())
        base = os.path.join(self.output_dir, f"{safe_filename(tag)}_{ts}")

        data, ext = mesh.export(fmt)
        file_path = f"{base}.{ext}"
        with open(file_path, "wb") as f:
            f.write(data)

        png_path = f"{base}.png"
        try:
            png_data = render_to_png(mesh, width=480, height=480)
            with open(png_path, "wb") as f:
                f.write(png_data)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[3DModel] 预览渲染失败（不影响模型文件）: {exc}")
            png_path = ""

        html_path = f"{base}.html"
        try:
            html = generate_html_preview(mesh, title=f"{tag} · 3D 模型预览", fmt=fmt)
            with open(html_path, "w", encoding="utf-8") as f:
                f.write(html)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[3DModel] HTML 预览生成失败: {exc}")
            html_path = ""

        return mesh, file_path, png_path, html_path, fmt

    def _make_result(self, event: AstrMessageEvent, msg: str, file_path: str,
                     png_path: str, html_path: str, out_fmt: str):
        """按配置组装消息链结果（文本 + 预览图 + 模型文件 + HTML）。"""
        cfg = self.config
        chain = [Plain(msg)]

        if png_path and cfg.get("send_preview_image", True) and os.path.exists(png_path):
            chain.append(Plain("\n📷 预览："))
            chain.append(Image.fromFileSystem(png_path))

        if cfg.get("send_model_file", True) and os.path.exists(file_path):
            chain.append(Plain(f"\n📦 模型文件（{out_fmt.upper()}）："))
            chain.append(File(file=file_path, name=os.path.basename(file_path)))

        if cfg.get("send_html_preview", False) and html_path and os.path.exists(html_path):
            chain.append(Plain("\n🖥️ 3D 交互预览（浏览器打开可旋转查看）："))
            chain.append(File(file=html_path, name=os.path.basename(html_path)))

        return event.chain_result(chain)

    # ------------------------------------------------------------------ #
    # LLM 工具：AI 对话中自动调用
    # ------------------------------------------------------------------ #
    @filter.llm_tool()
    async def create_3d_model(self, event: AstrMessageEvent, prompt: str,
                              model_type: str = "vase", **params):
        """根据文字描述创建一个 3D 模型并保存文件。

        Args:
            prompt(string): 用户对模型的完整描述，如「一只蓝色花瓶」「刻着 ABC 的铭牌」
            model_type(string): 模型类型：cube/sphere/cylinder/cone/torus/prism/pyramid/vase/gear/spring/terrain/text/heart/moebius/torus_knot/sierpinski
        """
        # 本地生成
        if not model_type or resolve_name(model_type) is None:
            model_type = "vase"
        resolved = resolve_name(model_type)
        try:
            mesh, file_path, _, _, out_fmt = await asyncio.to_thread(
                self._build_local, resolved, dict(params)
            )
        except Exception as exc:  # noqa: BLE001
            logger.error(f"[3DModel] LLM 工具生成失败: {exc}")
            yield event.plain_result(f"3D 模型生成失败：{exc}")
            return

        size_kb = os.path.getsize(file_path) / 1024
        yield event.plain_result(
            f"已生成【{resolved}】3D 模型：\n"
            f"- 顶点 {mesh.vertex_count:,}，三角面 {mesh.face_count:,}\n"
            f"- 文件：{file_path}（{out_fmt.upper()}，{size_kb:.1f} KB）\n"
            f"- 预览与文件已同步发送到当前会话"
        )
