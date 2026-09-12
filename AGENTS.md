# pal-oled 维护笔记

## 热重载（不用重启 Pal）

- 改了插件代码：`plugin_attach(name="oled_status")` 会重建 sidecar。
  有 manifest 变更（`plugin.toml`）先 `plugin_rescan`。
- **detach 之后重新挂载用 `plugin_attach`**，不是 control 侧的 attach ——
  后者只设 mounted 标志，不会真的把 daemon 拉起来。
- 换插件（emotion ↔ status）前确认旧 sidecar 真退了：
  `pgrep -af 'oled_status/sidecar.py'`。两个一起跑会互抢 I2C 总线。

## 排查顺序

1. 显示侧：`~/.pal/data/oled_status/oled.log`（正常帧率/状态更新/天气都会记），
   以及同目录的 `sidecar_stderr.log`。
2. 插件侧：`~/.pal/pal.sqlite3` 里 `plugin_bundles.last_error` —— 插件 `start()`
   抛的异常真身在这儿，工具面往往只看到「sidecar did not start within timeout」。
3. sidecar 起不来、但日志里出现过 socket listening → 大概率是渲染线程段错误。
   保持单线程，别为了「性能」开线程。

## 别踩的坑

- **顶层模块别叫 `introspection.py` / `sidecar.py`。** Python 按模块名缓存，
  会和别的插件撞车 —— 拿到的 handle 是别人家的，报
  `manifest module_id ... != handle ...`。本仓库用的是 `status_provider.py`。
- **emoji 和中文画不出来。** DejaVuSansMono 没有那些字形，屏上是一排一模一样的空盒。
  天气描述用英文、标签用 ASCII。
- **别用 `datetime.now()` 画时钟。** 主机的时区不是老爹的时区。
- 行宽上限 21 字符（10px 等宽 / 128px 屏）。超了会顶出边界。

## 自测（不接硬件）

```bash
# 两屏离线渲染成 PNG
python3 oled_status/sidecar.py --png-out /tmp/oled --timezone Asia/Shanghai

# 组装逻辑：用假 context 喂端口调 compose_status，验字段和降级
```

`compose_status` 在空 context 下不抛异常、字段留空 —— 单个端口坏掉不该把屏带崩。
