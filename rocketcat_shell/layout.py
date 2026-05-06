from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(slots=True)
class ProjectLayout:
    project_root: Path
    package_root: Path
    config_dir: Path
    plugins_config_dir: Path
    data_dir: Path
    bots_dir: Path
    plugins_dir: Path
    plugin_data_dir: Path
    logs_dir: Path
    shell_settings_path: Path
    bot_registry_path: Path
    log_file_path: Path

    @classmethod
    def discover(cls) -> "ProjectLayout":
        package_root = Path(__file__).resolve().parent
        project_root = package_root.parent
        config_dir = project_root / "config"
        plugins_config_dir = config_dir / "plugins_config"
        data_dir = project_root / "data"
        bots_dir = data_dir / "bots"
        plugins_dir = data_dir / "plugins"
        plugin_data_dir = data_dir / "plugin_data"
        logs_dir = project_root / "logs"
        return cls(
            project_root=project_root,
            package_root=package_root,
            config_dir=config_dir,
            plugins_config_dir=plugins_config_dir,
            data_dir=data_dir,
            bots_dir=bots_dir,
            plugins_dir=plugins_dir,
            plugin_data_dir=plugin_data_dir,
            logs_dir=logs_dir,
            shell_settings_path=config_dir / "shell.json",
            bot_registry_path=config_dir / "bots.json",
            log_file_path=logs_dir / "rocketcat.log",
        )

    def ensure_directories(self) -> None:
        self.config_dir.mkdir(parents=True, exist_ok=True)
        self.plugins_config_dir.mkdir(parents=True, exist_ok=True)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.bots_dir.mkdir(parents=True, exist_ok=True)
        self.plugins_dir.mkdir(parents=True, exist_ok=True)
        self.plugin_data_dir.mkdir(parents=True, exist_ok=True)
        self.logs_dir.mkdir(parents=True, exist_ok=True)