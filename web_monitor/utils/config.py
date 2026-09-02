import os
import yaml
import sys

from utils.config_paths import absolute_config_path, normalize_config_paths, resolve_path

def load_config(config_path=None):
    """Load and process configuration from config.yaml"""
    config_path = config_path or os.path.join(
        os.path.dirname(__file__), '..', '..', 'config.yaml'
    )
    config_path = absolute_config_path(config_path)
    config_dir = os.path.dirname(config_path)
    try:
        with open(config_path, 'r', encoding='utf-8') as f:
            config = normalize_config_paths(yaml.safe_load(f) or {}, config_path)
            DATABASE_PATH = config.get('database', {}).get('path') or resolve_path(
                './db.sqlite', base_dir=config_dir
            )

            recordings_path_raw = config.get('paths', {}).get('recordings_path', './recordings')
            RECORDINGS_PATH = resolve_path(recordings_path_raw, base_dir=config_dir)
            recordings_fav_path_raw = config.get('paths', {}).get('recordings_fav_path')
            RECORDINGS_FAV_PATH = resolve_path(recordings_fav_path_raw, base_dir=config_dir)
            favorite_source_path_raw = config.get('persistent_live_system', {}).get(
                'compressed_output_path', recordings_path_raw
            )
            FAVORITE_SOURCE_PATH = resolve_path(favorite_source_path_raw, base_dir=config_dir)

            return config, DATABASE_PATH, RECORDINGS_PATH, RECORDINGS_FAV_PATH, FAVORITE_SOURCE_PATH
    except (FileNotFoundError, yaml.YAMLError) as e:
        print(f"ERROR: Configuration error: {e}")
        sys.exit(1)
