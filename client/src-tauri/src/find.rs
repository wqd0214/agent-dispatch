//! 查找 agent-dispatch 可执行文件的纯逻辑，便于单元测试。

use std::path::{Path, PathBuf};

/// 常见安装位置候选：$HOME/.local/bin、/usr/local/bin、/opt/homebrew/bin、$HOME/bin
pub fn common_candidates(home: &str) -> Vec<PathBuf> {
    let home = home.trim_end_matches('/');
    vec![
        PathBuf::from(format!("{home}/.local/bin/agent-dispatch")),
        PathBuf::from("/usr/local/bin/agent-dispatch"),
        PathBuf::from("/opt/homebrew/bin/agent-dispatch"),
        PathBuf::from(format!("{home}/bin/agent-dispatch")),
    ]
}

/// 按顺序返回第一个「存在且可执行」的候选路径
pub fn pick_existing<I, F>(candidates: I, is_exec: F) -> Option<PathBuf>
where
    I: IntoIterator,
    I::Item: Into<PathBuf>,
    F: Fn(&Path) -> bool,
{
    candidates.into_iter().map(Into::into).find(|p| is_exec(p))
}

/// 从 shell 的 `command -v` 输出里取最后一个「存在且可执行」的绝对路径。
/// 交互 shell（-ic）可能额外打印提示文字，所以只看以 / 开头的行并要求文件真实存在。
pub fn pick_from_shell_output<F>(output: &str, is_exec: F) -> Option<PathBuf>
where
    F: Fn(&Path) -> bool,
{
    output.lines().rev().flat_map(|line| line.split_whitespace().rev()).find_map(|tok| {
        if !tok.starts_with('/') {
            return None;
        }
        let p = Path::new(tok);
        if is_exec(p) { Some(p.to_path_buf()) } else { None }
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::collections::HashSet;

    fn exists_set<'a>(paths: &'a [&'a str]) -> impl Fn(&Path) -> bool + 'a {
        let set: HashSet<String> = paths.iter().map(|s| s.to_string()).collect();
        move |p: &Path| set.contains(p.to_str().unwrap_or(""))
    }

    #[test]
    fn candidates_cover_common_locations_in_order() {
        let c = common_candidates("/Users/amy");
        assert_eq!(c[0], PathBuf::from("/Users/amy/.local/bin/agent-dispatch"));
        assert_eq!(c[1], PathBuf::from("/usr/local/bin/agent-dispatch"));
        assert_eq!(c[2], PathBuf::from("/opt/homebrew/bin/agent-dispatch"));
        assert_eq!(c[3], PathBuf::from("/Users/amy/bin/agent-dispatch"));
    }

    #[test]
    fn candidates_strips_trailing_slash_from_home() {
        let c = common_candidates("/Users/amy/");
        assert_eq!(c[0], PathBuf::from("/Users/amy/.local/bin/agent-dispatch"));
    }

    #[test]
    fn pick_existing_takes_first_match() {
        let is_exec = exists_set(&["/usr/local/bin/agent-dispatch"]);
        let c = common_candidates("/Users/amy");
        assert_eq!(pick_existing(c, is_exec), Some(PathBuf::from("/usr/local/bin/agent-dispatch")));
    }

    #[test]
    fn pick_existing_none_when_nothing_executable() {
        assert_eq!(pick_existing(common_candidates("/Users/amy"), exists_set(&[])), None);
    }

    #[test]
    fn shell_output_single_line_absolute_path() {
        let is_exec = exists_set(&["/Users/amy/.local/bin/agent-dispatch"]);
        assert_eq!(
            pick_from_shell_output("/Users/amy/.local/bin/agent-dispatch\n", is_exec),
            Some(PathBuf::from("/Users/amy/.local/bin/agent-dispatch"))
        );
    }

    #[test]
    fn shell_output_ignores_noise_and_takes_last_existing() {
        // 交互 shell 可能打印额外文字；只信真实存在的绝对路径
        let is_exec = exists_set(&["/Users/amy/.local/bin/agent-dispatch"]);
        let out = "agent-dispatch is /Users/amy/.local/bin/agent-dispatch\nlast login: ...\n";
        assert_eq!(
            pick_from_shell_output(out, is_exec),
            Some(PathBuf::from("/Users/amy/.local/bin/agent-dispatch"))
        );
    }

    #[test]
    fn shell_output_relative_path_rejected() {
        let is_exec = exists_set(&[".local/bin/agent-dispatch"]);
        assert_eq!(pick_from_shell_output(".local/bin/agent-dispatch\n", is_exec), None);
    }

    #[test]
    fn shell_output_nonexistent_path_rejected() {
        let is_exec = exists_set(&[]);
        assert_eq!(pick_from_shell_output("/opt/homebrew/bin/agent-dispatch\n", is_exec), None);
    }

    #[test]
    fn shell_output_empty() {
        assert_eq!(pick_from_shell_output("", exists_set(&["/bin/ls"])), None);
    }
}
