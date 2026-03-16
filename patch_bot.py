import os
import sys
import time
import base64
import argparse
import logging
import difflib
from typing import Any, Dict, List, Optional, Tuple

import gitlab
from fnmatch2 import fnmatch2
from ruamel.yaml import YAML

# ---- YAML (round-trip friendly) ----
yaml = YAML()
yaml.preserve_quotes = True
yaml.indent(mapping=2, sequence=4, offset=2)


# -----------------------------
# Logging
# -----------------------------
def build_logger(level: str) -> logging.Logger:
    log = logging.getLogger("sec-bot")
    log.setLevel(getattr(logging, level.upper(), logging.INFO))
    h = logging.StreamHandler(sys.stdout)
    h.setFormatter(logging.Formatter("[%(levelname)s] %(message)s"))
    log.handlers = [h]
    return log


# -----------------------------
# YAML helpers
# -----------------------------
def dump_yaml_to_str(data: Any) -> str:
    from io import StringIO
    buf = StringIO()
    yaml.dump(data, buf)
    return buf.getvalue()


def validate_yaml(text: str) -> bool:
    try:
        _ = yaml.load(text)
        return True
    except Exception:
        return False


def safe_load_yaml(text: str) -> Any:
    try:
        return yaml.load(text) or {}
    except Exception as e:
        raise ValueError(f"invalid YAML: {e}")


# -----------------------------
# Env/CI helpers
# -----------------------------
def env_truthy(v: Optional[str]) -> Optional[bool]:
    if v is None:
        return None
    s = str(v).strip().lower()
    if s == "":
        return None
    if s in ("1", "true", "yes", "y", "on"):
        return True
    if s in ("0", "false", "no", "n", "off"):
        return False
    return None


def env_list_csv(v: Optional[str]) -> List[str]:
    if not v:
        return []
    return [x.strip() for x in v.split(",") if x.strip()]


def normalize_envs(envs: str) -> List[str]:
    allowed = {"dev", "int", "qua", "prod", "prd", "qualiso"}

    if not envs:
        return sorted(list(allowed))

    s = envs.strip().lower()
    if s == "all":
        return sorted(list(allowed))

    parts = [p.strip() for p in s.split(",") if p.strip()]
    out = [p for p in parts if p in allowed]
    return out if out else sorted(list(allowed))


def filter_globs_by_env(globs: List[str], envs: List[str]) -> List[str]:
    out: List[str] = []
    known_envs = ["dev", "int", "qua", "prod", "prd", "qualiso"]

    def has_env_segment(glob_lower: str, env_name: str) -> bool:
        return f"/{env_name}/" in glob_lower or f"\\{env_name}\\" in glob_lower

    selected = set([e.strip().lower() for e in envs if e.strip()])

    for g in globs:
        gl = g.lower()
        matched_envs = [env for env in known_envs if has_env_segment(gl, env)]

        if not matched_envs:
            out.append(g)
            continue

        if any(env in selected for env in matched_envs):
            out.append(g)

    return out


# -----------------------------
# Diff helpers
# -----------------------------
def build_diff(old_text: str, new_text: str, file_path: str) -> str:
    diff = difflib.unified_diff(
        old_text.splitlines(),
        new_text.splitlines(),
        fromfile=f"{file_path} (before)",
        tofile=f"{file_path} (after)",
        lineterm="",
    )
    return "\n".join(diff)


def truncate_text(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + "\n... [truncated] ..."


# -----------------------------
# GitLab helpers
# -----------------------------
def get_file(project, file_path: str, ref: str) -> Optional[str]:
    try:
        f = project.files.get(file_path=file_path, ref=ref)
        return base64.b64decode(f.content).decode("utf-8", errors="replace")
    except gitlab.exceptions.GitlabGetError:
        return None


def ensure_branch(project, branch: str, ref: str, dry_run: bool, log: logging.Logger) -> bool:
    try:
        project.branches.get(branch)
        return True
    except gitlab.exceptions.GitlabGetError:
        if dry_run:
            log.info(f"[DRY-RUN] Would create branch {branch} from {ref}")
            return True
        try:
            project.branches.create({"branch": branch, "ref": ref})
            log.info(f"[BRANCH] created {branch} from {ref}")
            return True
        except Exception as e:
            log.error(f"[BRANCH-FAIL] create {branch} from {ref}: {e}")
            return False


def project_matches_any(patterns: List[str], path_with_namespace: str) -> bool:
    return any(fnmatch2(path_with_namespace, p) for p in patterns)


def resolve_target_branch(project, cfg_default_branch: str) -> str:
    forced = (os.getenv("TARGET_BRANCH") or "").strip()
    if forced:
        return forced

    project_default = getattr(project, "default_branch", None)
    if project_default:
        return project_default

    return cfg_default_branch or "main"


# -----------------------------
# Patch logic
# -----------------------------
def patch_chart_yaml(
    chart_text: str,
    policy_deps: Dict[str, str],
    allowed_deps: Optional[List[str]] = None,
) -> Tuple[str, bool, List[str]]:
    data = safe_load_yaml(chart_text)
    changed = False
    notes: List[str] = []

    deps = data.get("dependencies", [])
    chart_target_version: Optional[str] = None

    if isinstance(deps, list):
        for dep in deps:
            if not isinstance(dep, dict):
                continue

            name = str(dep.get("name", "")).strip()
            if not name:
                continue

            if allowed_deps is not None and name not in allowed_deps:
                continue

            if name not in policy_deps:
                continue

            cur = str(dep.get("version", "")).strip()
            tgt = str(policy_deps[name]).strip()

            if cur and cur != tgt:
                dep["version"] = tgt
                changed = True
                chart_target_version = tgt
                notes.append(f"dep {name}: {cur} -> {tgt}")

    if changed and chart_target_version:
        cur_chart_version = str(data.get("version", "")).strip()
        if cur_chart_version != chart_target_version:
            data["version"] = chart_target_version
            notes.append(f"chart version: {cur_chart_version} -> {chart_target_version}")

    if not changed:
        return chart_text, False, notes

    out = dump_yaml_to_str(data)
    return out, True, notes


def patch_values_yaml(
    values_text: str,
    images_policy: Dict[str, Any],
    ignore_latest: bool,
) -> Tuple[str, bool, List[str]]:
    data = safe_load_yaml(values_text)
    changed = False
    notes: List[str] = []

    def normalize_str(v: Any) -> str:
        return str(v).strip() if v is not None else ""

    def is_latest_tag(tag_val: Any) -> bool:
        return isinstance(tag_val, str) and tag_val.strip().lower() == "latest"

    def find_image_policy(repo: Optional[str], name: Optional[str]) -> Optional[Tuple[str, Dict[str, Any]]]:
        repo_norm = normalize_str(repo)
        name_norm = normalize_str(name)

        for img_name, spec in images_policy.items():
            spec_repos = [normalize_str(x) for x in (spec.get("repositories", []) or [])]
            spec_names = [normalize_str(x) for x in (spec.get("names", []) or [])]

            repo_match = repo_norm != "" and repo_norm in spec_repos
            name_match = name_norm != "" and name_norm in spec_names

            if repo_match or name_match:
                return img_name, spec

        return None

    def apply_tag_patch(
        repo: Optional[str],
        name: Optional[str],
        cur_tag: Any,
        set_tag_fn,
        path_prefix: str,
    ):
        nonlocal changed

        match = find_image_policy(repo=repo, name=name)
        if not match:
            return

        img_name, spec = match
        tgt = normalize_str(spec.get("tag", ""))

        if not tgt:
            return

        if is_latest_tag(cur_tag) and ignore_latest:
            notes.append(f"{path_prefix}tag latest ignored ({img_name})")
            return

        cur_norm = normalize_str(cur_tag)
        if cur_norm != tgt:
            set_tag_fn(tgt)
            changed = True

            identity_parts = []
            if repo:
                identity_parts.append(f"repository={repo}")
            if name:
                identity_parts.append(f"name={name}")

            identity = ", ".join(identity_parts) if identity_parts else img_name
            notes.append(f"{path_prefix}tag ({img_name}; {identity}): {cur_tag} -> {tgt}")

    def walk(obj: Any, path: str = ""):
        if isinstance(obj, dict):
            if "image" in obj and isinstance(obj["image"], dict):
                img = obj["image"]
                repo = img.get("repository")
                name = img.get("name")
                tag = img.get("tag")

                if "tag" in img and ("repository" in img or "name" in img):
                    apply_tag_patch(
                        repo=repo if isinstance(repo, str) else None,
                        name=name if isinstance(name, str) else None,
                        cur_tag=tag,
                        set_tag_fn=lambda v: img.__setitem__("tag", v),
                        path_prefix=f"{path}image.",
                    )

            repo = obj.get("repository")
            name = obj.get("name")
            tag = obj.get("tag")

            if "tag" in obj and ("repository" in obj or "name" in obj):
                apply_tag_patch(
                    repo=repo if isinstance(repo, str) else None,
                    name=name if isinstance(name, str) else None,
                    cur_tag=tag,
                    set_tag_fn=lambda v: obj.__setitem__("tag", v),
                    path_prefix=f"{path}",
                )

            for k, v in obj.items():
                walk(v, f"{path}{k}.")
        elif isinstance(obj, list):
            for i, it in enumerate(obj):
                walk(it, f"{path}[{i}].")

    walk(data, "")

    if not changed:
        return values_text, False, notes

    out = dump_yaml_to_str(data)
    return out, True, notes


# -----------------------------
# Commit via GitLab API
# -----------------------------
def commit_actions(
    project,
    branch: str,
    actions: List[Dict[str, Any]],
    commit_message: str,
    dry_run: bool,
    log: logging.Logger,
) -> bool:
    if dry_run:
        log.info(f"[DRY-RUN] Would commit {len(actions)} file(s) on {branch}")
        return True
    try:
        project.commits.create(
            {
                "branch": branch,
                "commit_message": commit_message,
                "actions": actions,
            }
        )
        log.info(f"[COMMIT] success ({len(actions)} file(s))")
        return True
    except Exception as e:
        log.error(f"[COMMIT-FAIL] {e}")
        return False


# -----------------------------
# MR upsert
# -----------------------------
def upsert_mr(
    project,
    source_branch: str,
    target_branch: str,
    title: str,
    description: str,
    labels: List[str],
    dry_run: bool,
    log: logging.Logger,
) -> Optional[str]:
    if dry_run:
        log.info(f"[DRY-RUN] Would open/update MR {source_branch} -> {target_branch}")
        return None

    try:
        mrs = project.mergerequests.list(
            source_branch=source_branch,
            target_branch=target_branch,
            state="opened",
            all=True,
        )
        if mrs:
            mr = project.mergerequests.get(mrs[0].iid)
            mr.title = title
            mr.description = description
            if labels:
                mr.labels = labels
            mr.save()
            log.info(f"[MR] updated (iid={mr.iid})")
            return getattr(mr, "web_url", None)
        else:
            mr = project.mergerequests.create(
                {
                    "source_branch": source_branch,
                    "target_branch": target_branch,
                    "title": title,
                    "description": description,
                    "labels": labels,
                }
            )
            log.info(f"[MR] created (iid={mr.iid})")
            return getattr(mr, "web_url", None)
    except Exception as e:
        log.error(f"[MR-FAIL] {e}")
        return None


# -----------------------------
# Scope discovery
# -----------------------------
def discover_projects(
    gl: gitlab.Gitlab,
    cfg: Dict[str, Any],
    log: logging.Logger,
) -> List[Any]:
    git_cfg = cfg["gitlab"]
    disc_cfg = cfg.get("discovery", {}) or {}

    scope = (os.getenv("SCOPE") or "group").strip().lower()
    include_subgroups = bool(disc_cfg.get("include_subgroups", True))

    exclude_projects = disc_cfg.get("exclude_projects", []) or []
    exclude_env = env_list_csv(os.getenv("EXCLUDE_PROJECTS"))
    if exclude_env:
        exclude_projects = exclude_projects + exclude_env

    if scope == "project":
        project_id_env = os.getenv("PROJECT_ID")
        project_path_env = os.getenv("PROJECT_PATH")
        if project_id_env:
            project = gl.projects.get(int(project_id_env))
        elif project_path_env:
            project = gl.projects.get(project_path_env)
        else:
            raise SystemExit("SCOPE=project requires PROJECT_ID or PROJECT_PATH")
        if exclude_projects and project_matches_any(exclude_projects, project.path_with_namespace):
            log.info(f"[SKIP] {project.path_with_namespace} (excluded)")
            return []
        return [project]

    group_id = os.getenv("GROUP_ID")
    if group_id:
        group = gl.groups.get(int(group_id))
    else:
        group = gl.groups.get(int(git_cfg["group_id"]))

    projects = group.projects.list(all=True, include_subgroups=include_subgroups)

    filtered = []
    for p in projects:
        p_path = getattr(p, "path_with_namespace", None)
        if p_path and exclude_projects and project_matches_any(exclude_projects, p_path):
            log.info(f"[SKIP] {p_path} (excluded)")
            continue
        filtered.append(p)

    return filtered


# -----------------------------
# Report helpers
# -----------------------------
def write_report(path: str, content: str, log: logging.Logger) -> None:
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        log.info(f"[REPORT] written {path}")
    except Exception as e:
        log.warning(f"[REPORT] cannot write {path}: {e}")


def render_project_report_md(
    path: str,
    target_branch: str,
    branch: str,
    changed_files: List[str],
    notes_by_file: Dict[str, List[str]],
    diffs_by_file: Dict[str, str],
) -> str:
    lines: List[str] = []
    lines.append(f"## Project: `{path}`")
    lines.append("")
    lines.append(f"- Target branch: `{target_branch}`")
    lines.append(f"- Working branch: `{branch}`")
    lines.append(f"- Files changed: **{len(changed_files)}**")
    lines.append("")

    for fp in changed_files:
        lines.append(f"### File: `{fp}`")
        lines.append("")
        for note in notes_by_file.get(fp, []):
            lines.append(f"- {note}")
        lines.append("")
        diff_text = diffs_by_file.get(fp, "")
        if diff_text:
            lines.append("```diff")
            lines.append(diff_text)
            lines.append("```")
            lines.append("")

    return "\n".join(lines)


# -----------------------------
# Main
# -----------------------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True)
    args = ap.parse_args()

    with open(args.config, "r", encoding="utf-8") as f:
        cfg = yaml.load(f) or {}

    log = build_logger(cfg.get("execution", {}).get("log_level", "INFO"))

    token = os.getenv("GITLAB_TOKEN")
    if not token:
        log.error("Missing GITLAB_TOKEN")
        sys.exit(2)

    git_cfg = cfg["gitlab"]
    gl = gitlab.Gitlab(git_cfg["url"], private_token=token)
    gl.auth()

    cfg_default_branch = git_cfg.get("default_branch", "main")
    labels = git_cfg.get("labels", []) or []

    dry_run_cfg = bool(cfg.get("execution", {}).get("dry_run", True))
    dry_run_env = env_truthy(os.getenv("DRY_RUN"))
    dry_run = dry_run_env if dry_run_env is not None else dry_run_cfg

    branch_prefix = (os.getenv("BRANCH_PREFIX") or git_cfg.get("branch_prefix", "sec/patch")).strip()

    envs = normalize_envs(os.getenv("ENVS") or "all")
    scope = (os.getenv("SCOPE") or "group").strip().lower()

    create_branch_if_missing = bool(cfg.get("execution", {}).get("create_branch_if_missing", True))
    mr_title_prefix = cfg.get("execution", {}).get("mr_title_prefix", "Security patches")

    ignore_latest = bool(cfg.get("rules", {}).get("ignore_latest", True))
    open_mr_only_if_changed = bool(cfg.get("rules", {}).get("open_mr_only_if_changed", True))
    allowed_deps = cfg.get("rules", {}).get("allowed_deps", None)

    policy_deps = cfg.get("policy", {}).get("helm_dependencies", {}) or {}
    images_policy = cfg.get("policy", {}).get("images", {}) or {}

    chart_globs = cfg.get("files", {}).get("chart_globs", ["Chart.yaml"])
    values_globs = cfg.get("files", {}).get("values_globs", ["values.yaml"])

    chart_globs_filtered = filter_globs_by_env(chart_globs, envs)
    values_globs_filtered = filter_globs_by_env(values_globs, envs)

    stamp = time.strftime("%Y%m%d-%H%M")
    branch = f"{branch_prefix}-{stamp}"

    target_hint = ""
    if scope == "project":
        target_hint = os.getenv("PROJECT_PATH") or os.getenv("PROJECT_ID") or "(missing)"
    else:
        target_hint = os.getenv("GROUP_ID") or str(git_cfg.get("group_id"))

    log.info(f"[START] scope={scope} target={target_hint} envs={','.join(envs)} dry_run={dry_run} branch={branch}")
    log.info(f"[GLOBS] chart_globs={chart_globs_filtered}")
    log.info(f"[GLOBS] values_globs={values_globs_filtered}")

    projects = discover_projects(gl, cfg, log)

    report_lines: List[str] = []
    report_lines.append("# Security Patch Report")
    report_lines.append("")
    report_lines.append(f"- Timestamp: `{stamp}`")
    report_lines.append(f"- Scope: `{scope}`")
    report_lines.append(f"- Target input: `{target_hint}`")
    report_lines.append(f"- Envs: `{','.join(envs)}`")
    report_lines.append(f"- Dry-run: `{dry_run}`")
    report_lines.append("")

    total_projects_scanned = 0
    total_projects_changed = 0
    total_files_changed = 0
    total_changes = 0

    for p in projects:
        project = gl.projects.get(p.id) if hasattr(p, "id") else p
        path = project.path_with_namespace
        target_branch = resolve_target_branch(project, cfg_default_branch)

        total_projects_scanned += 1
        log.info(f"[SCAN] {path} target_branch={target_branch} envs={','.join(envs)} dry_run={dry_run}")

        try:
            tree = project.repository_tree(recursive=True, all=True, ref=target_branch)
        except Exception as e:
            log.warning(f"{path}: cannot read tree on ref={target_branch}: {e}")
            report_lines.append(f"## Project: `{path}`")
            report_lines.append("")
            report_lines.append(f"- Target branch: `{target_branch}`")
            report_lines.append(f"- Error: cannot read repository tree: `{e}`")
            report_lines.append("")
            continue

        chart_candidates = [
            it["path"]
            for it in tree
            if it.get("type") == "blob" and any(fnmatch2(it["path"], g) for g in chart_globs_filtered)
        ]
        values_candidates = [
            it["path"]
            for it in tree
            if it.get("type") == "blob" and any(fnmatch2(it["path"], g) for g in values_globs_filtered)
        ]

        actions: List[Dict[str, Any]] = []
        changed_files: List[str] = []
        notes_by_file: Dict[str, List[str]] = {}
        diffs_by_file: Dict[str, str] = {}

        for cp in chart_candidates:
            txt = get_file(project, cp, target_branch)
            if txt is None:
                continue
            try:
                new_txt, changed, notes = patch_chart_yaml(txt, policy_deps, allowed_deps)
            except Exception as e:
                log.warning(f"{path}: skip {cp} (chart parse error): {e}")
                continue

            if changed:
                if not validate_yaml(new_txt):
                    log.warning(f"{path}: skip {cp} (invalid YAML after patch)")
                    continue
                diff_text = build_diff(txt, new_txt, cp)
                actions.append({"action": "update", "file_path": cp, "content": new_txt})
                changed_files.append(cp)
                notes_by_file[cp] = notes
                diffs_by_file[cp] = diff_text

        for vp in values_candidates:
            txt = get_file(project, vp, target_branch)
            if txt is None:
                continue
            try:
                new_txt, changed, notes = patch_values_yaml(txt, images_policy, ignore_latest)
            except Exception as e:
                log.warning(f"{path}: skip {vp} (values parse error): {e}")
                continue

            if changed:
                if not validate_yaml(new_txt):
                    log.warning(f"{path}: skip {vp} (invalid YAML after patch)")
                    continue
                diff_text = build_diff(txt, new_txt, vp)
                actions.append({"action": "update", "file_path": vp, "content": new_txt})
                changed_files.append(vp)
                notes_by_file[vp] = notes
                diffs_by_file[vp] = diff_text

        report_lines.append(f"## Project: `{path}`")
        report_lines.append("")
        report_lines.append(f"- Target branch: `{target_branch}`")
        report_lines.append(f"- Working branch: `{branch}`")
        report_lines.append(f"- Candidates: charts={len(chart_candidates)}, values={len(values_candidates)}")

        if open_mr_only_if_changed and not actions:
            log.info(f"[NO-CHANGE] {path}")
            report_lines.append("- Result: no changes")
            report_lines.append("")
            continue

        total_projects_changed += 1
        unique_files = sorted(set(changed_files))
        project_change_count = sum(len(v) for v in notes_by_file.values())

        total_files_changed += len(unique_files)
        total_changes += project_change_count

        report_lines.append(f"- Result: changes detected")
        report_lines.append(f"- Files to patch: **{len(unique_files)}**")
        report_lines.append(f"- Changes: **{project_change_count}**")
        report_lines.append("")

        report_lines.append(
            render_project_report_md(
                path=path,
                target_branch=target_branch,
                branch=branch,
                changed_files=unique_files,
                notes_by_file=notes_by_file,
                diffs_by_file=diffs_by_file,
            )
        )
        report_lines.append("")

        if create_branch_if_missing:
            ok_branch = ensure_branch(project, branch, target_branch, dry_run, log)
            if not ok_branch:
                continue

        commit_msg = f"security: align charts/values ({stamp})"
        ok_commit = commit_actions(project, branch, actions, commit_msg, dry_run, log)
        if not ok_commit:
            sys.exit(1)

        title = f"{mr_title_prefix} ({stamp})"

        desc_lines: List[str] = []
        desc_lines.append("## Automated security alignment")
        desc_lines.append("")
        desc_lines.append(f"- Project: `{path}`")
        desc_lines.append(f"- Target branch: `{target_branch}`")
        desc_lines.append(f"- Working branch: `{branch}`")
        desc_lines.append(f"- Scope: `{scope}`")
        desc_lines.append(f"- Envs: `{','.join(envs)}`")
        desc_lines.append(f"- Changed files: **{len(unique_files)}**")
        desc_lines.append("")
        desc_lines.append("### Files")
        for fpath in unique_files:
            desc_lines.append(f"- `{fpath}`")
        desc_lines.append("")

        desc_lines.append("### Changes")
        for fpath in unique_files:
            desc_lines.append(f"#### `{fpath}`")
            for n in notes_by_file.get(fpath, []):
                desc_lines.append(f"- {n}")
            desc_lines.append("")

        desc_lines.append("### Diff Preview")
        for fpath in unique_files:
            diff_preview = truncate_text(diffs_by_file.get(fpath, ""), 3000)
            if diff_preview:
                desc_lines.append(f"#### `{fpath}`")
                desc_lines.append("```diff")
                desc_lines.append(diff_preview)
                desc_lines.append("```")
                desc_lines.append("")

        desc = "\n".join(desc_lines)

        mr_url = upsert_mr(
            project=project,
            source_branch=branch,
            target_branch=target_branch,
            title=title,
            description=desc,
            labels=labels,
            dry_run=dry_run,
            log=log,
        )
        if mr_url:
            log.info(f"[MR] {mr_url}")
            report_lines.append(f"- MR: {mr_url}")
            report_lines.append("")

        if dry_run:
            log.info("")
            log.info(f"=== SECURITY PATCH REPORT ({path}) ===")
            for fpath in unique_files:
                log.info(f"File: {fpath}")
                for n in notes_by_file.get(fpath, []):
                    log.info(f"  - {n}")
            log.info(f"Summary: {len(unique_files)} file(s), {project_change_count} change(s)")
            log.info("")

    report_lines.append("## Summary")
    report_lines.append("")
    report_lines.append(f"- Projects scanned: **{total_projects_scanned}**")
    report_lines.append(f"- Projects with changes: **{total_projects_changed}**")
    report_lines.append(f"- Files to patch (total): **{total_files_changed}**")
    report_lines.append(f"- Changes (total): **{total_changes}**")
    report_lines.append("")

    report_content = "\n".join(report_lines)
    report_path = os.getenv("REPORT_PATH") or "report.md"
    write_report(report_path, report_content, log)

    log.info(
        f"[SUMMARY] projects_scanned={total_projects_scanned} "
        f"projects_changed={total_projects_changed} "
        f"files={total_files_changed} changes={total_changes}"
    )
    log.info(f"Done. dry_run={dry_run}")


if __name__ == "__main__":
    main()
