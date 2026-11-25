"""上传目录与文件路径解析（修复 P0 #2）

背景：`upload_cache` 之前用的是 `Path("./upload_cache")`，落库的 `file_path`
就是 `./upload_cache/xxx.pdf` 这种**相对路径**。API 进程和 Celery worker 的 CWD
一旦不同（README 里两者都要求从 `src/` 启动，实际部署很容易不一致），
worker 就去一个不存在的路径找文件，解析必然失败。

本模块做三件事：
1. 上传目录来自 `config/config.yaml` 的 `upload.dir`（D1），并解析成**绝对路径**
   （相对路径按仓库根解析 ⇒ 与 CWD 无关，API / worker 在任何目录下算法一致）；
2. 提供 `resolve_upload_path()` 把 DB 里的 `file_path` 解析成绝对路径，
   覆盖库里已有的相对路径老数据（D4：不刷数据，只在做读取侧兜底）；
3. 解析结果必须落在上传目录内，否则拒绝（防路径穿越、防误删其它目录）。
"""

from pathlib import Path
from typing import Optional

from settings import BASE_DIR, PROJECT_ROOT, resolve_path, settings

# 上传目录：绝对路径，与进程 CWD 无关
UPLOAD_DIR = resolve_path(settings.upload.dir)


def ensure_upload_dir() -> Path:
    """确保上传目录存在（parents=True：配成多级路径时不会失败）"""
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    return UPLOAD_DIR


def _inside_upload_dir(path: Path) -> bool:
    """路径是否落在上传目录内（含目录本身）"""
    try:
        resolved = path.resolve()
    except Exception:
        return False
    return resolved == UPLOAD_DIR or UPLOAD_DIR in resolved.parents


def resolve_upload_path(file_path) -> Optional[Path]:
    """把 DB / 参数里的 file_path 解析成上传目录下的绝对路径。

    规则：
      - 绝对路径：直接用（新数据都是绝对路径），但必须落在上传目录内；
      - 相对路径（老数据，形如 `upload_cache/xxx.pdf`）：依次尝试
        仓库根 / `src/`（旧的按 CWD 写入的位置）/ 上传目录 + 文件名，
        取第一个真实存在的；
      - 都不存在（例如文件已被删）：返回上传目录下的规范位置，由调用方处理"不存在"；
      - 越界（不在上传目录内）：告警并返回 None。
    """
    if not file_path:
        return None

    raw = Path(str(file_path))

    if raw.is_absolute():
        if not _inside_upload_dir(raw):
            print(f"[upload] file_path 不在上传目录内，已拒绝：{file_path}")
            return None
        return raw.resolve()

    candidates = [
        PROJECT_ROOT / raw,      # upload.dir 配成仓库根下其它位置时也对得上
        BASE_DIR / raw,          # 老数据：当时按 CWD=src 写入了 ./upload_cache/xxx
        UPLOAD_DIR / raw.name,   # 兜底：只认文件名，放到当前上传目录
    ]
    for cand in candidates:
        if _inside_upload_dir(cand) and cand.exists():
            return cand.resolve()

    # 文件不存在（查询/删除场景是正常的）：给出规范位置，保证不会跳出上传目录
    return UPLOAD_DIR / raw.name
