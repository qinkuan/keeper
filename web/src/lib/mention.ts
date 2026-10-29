/**
 * 输入框 `@` 快捷项：用户输入 `@` 时弹出一个可选项列表，选中后把对应短语插入消息。
 *
 * 纯前端配置（localStorage），不依赖后端 settings —— 这是输入体验层面的便利功能，
 * 不需要跨端同步。设置弹窗里的「输入 @ 快捷项」段负责增删改。
 *
 * 短语支持占位符：
 *  - `{user_space}`：我的空间的绝对路径（来自 settings.yaml 的 user_space.path，单空间）；
 *  - `{session_path}`：当前会话私有工作目录的绝对路径（agent_space）。
 */

export interface MentionItem {
  /** 稳定主键（设置里新增的项用时间戳生成），主要用于 React key */
  key: string;
  /** 弹窗里显示的昵称（用户输入 `@` 后匹配它） */
  label: string;
  /** 选中后插入到消息里的文本；可含 `{session_path}` 占位符 */
  snippet: string;
  /**
   * 文件类候选才有：「我的空间」里的文件。
   *
   * 快捷项插入的是**短语**（@我的空间 → 一句提示词），文件插入的是**引用**
   * （@方案.docx → 这个文件的绝对路径）。两类东西的展开方式不同，所以分开标记：
   * 快捷项在发送时展开成短语，文件则把绝对路径追加给模型。
   */
  filePath?: string;
  /** 文件类候选的相对路径（同名文件用它消歧，弹窗里当副标题显示） */
  fileRel?: string;
  /** 文件类候选是不是目录 */
  isDir?: boolean;
  /**
   * 文件类候选的**命中落点**（从检索结果来的时候才有）。
   *
   * 有了它，选中这个文件就能直接跳到命中的那一段 / 那一页，
   * 而不是"打开了但要自己找"。
   */
  fileLoc?: {
    /** docx：段序号；markdown：行号 */
    line?: number;
    /** pdf：页码 */
    page?: number;
    /** 命中词 */
    terms?: string[];
  };
}

export interface MentionConfig {
  /** `{session_path}` 解析用的根目录，末尾建议带 / */
  root: string;
  items: MentionItem[];
}

const KEY = "keeper.mention.v1";

export const DEFAULT_MENTION: MentionConfig = {
  root: "~/.keeper/workspace/session/",
  items: [
    {
      key: "myspace",
      label: "我的空间",
      snippet: "在我的空间，绝对路径: {user_space}",
    },
    {
      key: "session",
      label: "当前会话目录",
      snippet: "会话工作目录：{session_path}",
    },
  ],
};

/** 配置变更事件：设置面板保存后广播，让正在输入的会话实时刷新候选 */
export const MENTION_EVENT = "keeper:mention-updated";

export function loadMention(): MentionConfig {
  try {
    const raw = localStorage.getItem(KEY);
    if (!raw) return { ...DEFAULT_MENTION, items: [...DEFAULT_MENTION.items] };
    const p = JSON.parse(raw) as Partial<MentionConfig>;
    let items: MentionItem[] =
      Array.isArray(p.items) && p.items.length
        ? p.items
        : [...DEFAULT_MENTION.items];
    // 迁移：用户明确要求「我的空间」指向默认用户空间，而非会话私有目录。
    // 把默认项的 {session_path} 就地改成 {user_space}（幂等，不影响其它项）。
    items = items.map((it) =>
      it.label === "我的空间" && it.snippet.includes("{session_path}")
        ? { ...it, snippet: it.snippet.replace(/\{session_path\}/g, "{user_space}") }
        : it
    );
    return {
      root: p.root ?? DEFAULT_MENTION.root,
      items,
    };
  } catch {
    return { ...DEFAULT_MENTION, items: [...DEFAULT_MENTION.items] };
  }
}

export function saveMention(cfg: MentionConfig) {
  localStorage.setItem(KEY, JSON.stringify(cfg));
  window.dispatchEvent(new Event(MENTION_EVENT));
}

/** 解析占位符：
 *  - `{user_space}`  → 我的空间的绝对路径（settings.yaml 的 user_space.path，单空间）；
 *  - `{session_path}` → 当前会话私有工作目录的绝对路径（agent_space）。
 *  调用方负责传入真实绝对路径。 */
export function resolveSnippet(
  snippet: string,
  paths: { sessionPath: string; userSpacePath: string }
): string {
  return snippet
    .replace(/\{session_path\}/g, paths.sessionPath || "")
    .replace(/\{user_space\}/g, paths.userSpacePath || "");
}

/** 按用户输入的查询串过滤候选（匹配 label 或 key，忽略大小写） */
export function filterMentionItems(
  items: MentionItem[],
  query: string
): MentionItem[] {
  const q = query.trim().toLowerCase();
  if (!q) return items;
  return items.filter(
    (i) =>
      i.label.toLowerCase().includes(q) || i.key.toLowerCase().includes(q)
  );
}
