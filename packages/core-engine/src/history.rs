//! Command 模式 Undo/Redo 栈（契约 2.6.3）：全工程状态可逆。
//!
//! 简化策略：快照式——每次 mutation 前压入完整工程 JSON。
//! 工程体量（数百音符 + 稀疏曲线）下单快照 <1MB，100 步上限可控。

use serde::{Deserialize, Serialize};
use std::collections::VecDeque;

const MAX_HISTORY: usize = 100;

pub struct History<T: Clone + Serialize + for<'de> Deserialize<'de>> {
    undo_stack: VecDeque<String>,
    redo_stack: VecDeque<String>,
    _marker: std::marker::PhantomData<T>,
}

impl<T: Clone + Serialize + for<'de> Deserialize<'de>> Default for History<T> {
    fn default() -> Self {
        Self {
            undo_stack: VecDeque::new(),
            redo_stack: VecDeque::new(),
            _marker: std::marker::PhantomData,
        }
    }
}

impl<T: Clone + Serialize + for<'de> Deserialize<'de>> History<T> {
    /// mutation 前调用：快照当前状态。
    pub fn push(&mut self, state: &T) {
        let snap = serde_json::to_string(state).unwrap_or_default();
        self.undo_stack.push_back(snap);
        if self.undo_stack.len() > MAX_HISTORY {
            self.undo_stack.pop_front();
        }
        self.redo_stack.clear();
    }

    /// undo：返回上一个状态；无历史返回 None。
    pub fn undo(&mut self, current: &T) -> Option<T> {
        let snap = self.undo_stack.pop_back()?;
        self.redo_stack
            .push_back(serde_json::to_string(current).unwrap_or_default());
        serde_json::from_str(&snap).ok()
    }

    pub fn redo(&mut self, current: &T) -> Option<T> {
        let snap = self.redo_stack.pop_back()?;
        self.undo_stack
            .push_back(serde_json::to_string(current).unwrap_or_default());
        serde_json::from_str(&snap).ok()
    }

    pub fn can_undo(&self) -> bool {
        !self.undo_stack.is_empty()
    }

    pub fn can_redo(&self) -> bool {
        !self.redo_stack.is_empty()
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn undo_redo_roundtrip() {
        #[derive(Clone, Serialize, serde::Deserialize)]
        struct Doc {
            v: u32,
        }
        let mut doc = Doc { v: 1 };
        let mut h = History::default();
        h.push(&doc);
        doc.v = 2;
        h.push(&doc);
        doc.v = 3;
        let d2 = h.undo(&doc).unwrap();
        assert_eq!(d2.v, 2);
        let d1 = h.undo(&d2).unwrap();
        assert_eq!(d1.v, 1);
        assert!(!h.can_undo());
        assert_eq!(h.redo(&d1).unwrap().v, 2);
    }

    #[test]
    fn push_clears_redo() {
        #[derive(Clone, Serialize, serde::Deserialize)]
        struct Doc {
            v: u32,
        }
        let mut doc = Doc { v: 1 };
        let mut h = History::default();
        h.push(&doc);
        doc.v = 2;
        let _ = h.undo(&doc).unwrap();
        h.push(&doc);
        assert!(!h.can_redo());
        let _ = json!(null); // silence unused import in cfg(test)
    }
}
