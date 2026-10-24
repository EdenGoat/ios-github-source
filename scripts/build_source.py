#!/usr/bin/env python3
"""Generate a SideStore/AltStore Classic source from public GitHub Release IPAs.

Only downloads an IPA when the selected release asset changes. Metadata cache is
committed by the workflow, so scheduled runs generally only query the GitHub API.
No Apple credentials or signing certificates are needed here.
"""
import copy
import datetime as dt
import json
import os
import plistlib
import re
import tempfile
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONF = ROOT / 'apps.json'
CACHE = ROOT / 'data/cache.json'
SITE = ROOT / 'site'
TOKEN = os.environ.get('GH_TOKEN', '')


def norm_repo(value):
    value = str(value).strip()
    value = re.sub(r'^https?://(?:www\.)?github\.com/', '', value, flags=re.I)
    value = value.strip('/').removesuffix('.git')
    if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', value):
        raise ValueError(f'仓库格式错误：{value!r}（需要 GitHub 地址或 owner/repo）')
    return value


def gh_api(path):
    h = {'User-Agent': 'GitHub-IPA-AltSource',
         'Accept': 'application/vnd.github+json',
         'X-GitHub-Api-Version': '2022-11-28'}
    if TOKEN:
        h['Authorization'] = f'Bearer {TOKEN}'
    req = urllib.request.Request('https://api.github.com/' + path, headers=h)
    with urllib.request.urlopen(req, timeout=40) as r:
        return json.load(r)


def choose_asset(repo, pattern, include_prerelease):
    releases = gh_api(f'repos/{repo}/releases?per_page=30')
    if not isinstance(releases, list):
        raise ValueError('GitHub API 未返回 Release 列表')
    matches = []
    for rel in releases:
        if rel.get('draft') or (rel.get('prerelease') and not include_prerelease):
            continue
        assets = [a for a in rel.get('assets', [])
                  if str(a.get('name', '')).lower().endswith('.ipa')
                  and re.search(pattern, str(a.get('name', '')))]
        if not assets:
            continue
        assets.sort(key=lambda a: str(a.get('name', '')).lower())
        if len(assets) > 1:
            print(f'  提示：{rel.get("tag_name")} 存在 {len(assets)} 个匹配 IPA；选取 {assets[0]["name"]}，可用 asset_regex 精确匹配')
        matches.append((rel, assets[0]))
    if not matches:
        return None
    # GitHub 的列表顺序未必严格等于发布时间，显式排序以避免 TV 专版干扰。
    matches.sort(key=lambda ra: (ra[0].get('published_at') or ra[0].get('created_at') or '',
                                  ra[0].get('id', 0)), reverse=True)
    return matches[0]


def read_ipa(ipa_url):
    req = urllib.request.Request(ipa_url, headers={'User-Agent': 'GitHub-IPA-AltSource'})
    with tempfile.NamedTemporaryFile(suffix='.ipa') as temp:
        with urllib.request.urlopen(req, timeout=120) as response:
            # Maximum 700MiB to avoid accidentally fetching unexpectedly huge files
            total = 0
            while True:
                block = response.read(1024 * 1024)
                if not block:
                    break
                total += len(block)
                if total > 700 * 1024 * 1024:
                    raise ValueError('IPA 文件超过 700MiB 限制')
                temp.write(block)
        temp.flush()
        with zipfile.ZipFile(temp.name) as zf:
            candidates = [p for p in zf.namelist()
                          if re.fullmatch(r'Payload/[^/]+\.app/Info\.plist', p)]
            if len(candidates) != 1:
                raise ValueError(f'IPA 内未找到唯一的主应用 Info.plist：{candidates}')
            info = plistlib.loads(zf.read(candidates[0]))
    for key in ('CFBundleIdentifier', 'CFBundleShortVersionString', 'CFBundleVersion'):
        if not info.get(key):
            raise ValueError(f'Info.plist 缺少 {key}')
    privacy = {str(k): str(v) for k, v in info.items()
               if k.startswith('NS') and k.endswith('UsageDescription') and isinstance(v, str)}
    return {
        'bundle': str(info['CFBundleIdentifier']),
        'name': str(info.get('CFBundleDisplayName') or info.get('CFBundleName') or ''),
        'version': str(info['CFBundleShortVersionString']),
        'build_version': str(info['CFBundleVersion']),
        'min_os': str(info.get('MinimumOSVersion', '') or ''),
        'privacy': privacy,
    }


def build():
    conf = json.loads(CONF.read_text(encoding='utf-8'))
    old_cache = json.loads(CACHE.read_text(encoding='utf-8')) if CACHE.exists() else {}
    new_cache, apps, seen_bundle = {}, [], set()
    count_errors = 0
    for raw in conf.get('apps', []):
        try:
            repo = norm_repo(raw['repo'])
            pattern = raw.get('asset_regex', r'(?i).*\.ipa$')
            re.compile(pattern)
            print(f'检查 {repo} ...', flush=True)
            selected = choose_asset(repo, pattern, raw.get('prerelease', conf.get('check_prerelease', False)))
            if not selected:
                print(f'  跳过：最近 30 个 Release 中无匹配 IPA')
                continue
            rel, asset = selected
            asset_id = asset['id']
            cached = old_cache.get(repo)
            if cached and cached.get('asset_id') == asset_id:
                app = copy.deepcopy(cached['app'])
                print(f'  使用缓存：{rel.get("tag_name")} / {asset["name"]}')
            else:
                print(f'  下载并检查 IPA：{rel.get("tag_name")} / {asset["name"]}', flush=True)
                meta = read_ipa(asset['browser_download_url'])
                ver = {
                    'version': meta['version'],
                    'buildVersion': meta['build_version'],
                    'date': (rel.get('published_at') or rel.get('created_at') or dt.datetime.now(dt.timezone.utc).isoformat())[:10],
                    'localizedDescription': str(rel.get('body') or '来自 GitHub Release')[:5000],
                    'downloadURL': asset['browser_download_url'],
                    'size': int(asset.get('size') or 0),
                }
                if meta['min_os']:
                    ver['minOSVersion'] = meta['min_os']
                history = cached.get('app', {}).get('versions', []) if cached else []
                history = [v for v in history if (v.get('version'), v.get('buildVersion')) !=
                           (ver['version'], ver['buildVersion'])]
                # If the buildVersion is unchanged, the AltStore client cannot flag an update.
                app = {
                    'name': raw.get('name') or meta['name'] or repo.split('/')[1],
                    'bundleIdentifier': meta['bundle'],
                    'developerName': raw.get('developer_name') or repo.split('/')[0],
                    'subtitle': f'GitHub：{repo}',
                    'localizedDescription': f'来自 https://github.com/{repo} 的 IPA。安装前请核实项目来源和签名。',
                    'iconURL': raw.get('icon_url') or f'https://github.com/{repo.split("/")[0]}.png?size=256',
                    'versions': [ver] + history[:max(0, int(conf.get('max_versions', 5))-1)],
                    'appPermissions': {'entitlements': [], 'privacy': meta['privacy']},
                }
                print(f'  解析：{meta["bundle"]} {meta["version"]} ({meta["build_version"]})')
            # Editable presentation fields can change without redownloading the IPA.
            if raw.get('name'):
                app['name'] = raw['name']
            if raw.get('developer_name'):
                app['developerName'] = raw['developer_name']
            app['iconURL'] = raw.get('icon_url') or f'https://github.com/{repo.split("/")[0]}.png?size=256'
            if app['bundleIdentifier'] in seen_bundle:
                print(f'  跳过：重复 Bundle ID {app["bundleIdentifier"]}')
                continue
            seen_bundle.add(app['bundleIdentifier'])
            apps.append(app)
            new_cache[repo] = {'asset_id': asset_id, 'app': app}
        except Exception as e:
            count_errors += 1
            print(f'  出错：{type(e).__name__}: {e}', flush=True)
            # Keep prior data if an API / download outage affects one repository.
            try:
                repo = norm_repo(raw['repo'])
                if repo in old_cache and old_cache[repo]['app']['bundleIdentifier'] not in seen_bundle:
                    saved = old_cache[repo]
                    new_cache[repo] = saved
                    apps.append(saved['app'])
                    seen_bundle.add(saved['app']['bundleIdentifier'])
                    print('  继续保留该应用上一次成功的版本')
            except (KeyError, ValueError):
                pass
    if not apps:
        raise SystemExit('没有任何有效的 IPA 应用。检查 apps.json 中仓库地址及工作流日志。')
    site = {
        'name': conf.get('source_name', 'My GitHub Apps'),
        'subtitle': conf.get('source_subtitle', 'GitHub IPA 更新源'),
        'description': conf.get('source_description', ''),
        'apps': apps,
        'news': [],
    }
    if any('jhentai' in a['name'].lower() for a in apps):
        site['nsfw'] = True
    SITE.mkdir(exist_ok=True)
    (SITE / 'source.json').write_text(json.dumps(site, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    (SITE / 'index.html').write_text('<!doctype html><html lang="zh"><meta charset="utf-8">'
                                      '<title>My GitHub Apps</title><h1>My GitHub Apps</h1>'
                                      '<p>请在 SideStore / Feather 中添加本目录下的 '
                                      '<a href="source.json">source.json</a>。</p></html>', encoding='utf-8')
    CACHE.parent.mkdir(exist_ok=True)
    CACHE.write_text(json.dumps(new_cache, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(f'完成：{len(apps)} 个应用，{count_errors} 个本轮抓取异常；生成 site/source.json')


if __name__ == '__main__':
    build()
