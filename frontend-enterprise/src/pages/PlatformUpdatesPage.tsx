import AppHeader from '@/components/AppHeader';
import { MarkdownMessage } from '@/pages/chat/chatHelpers';

// 直接以原始文本引入仓库根目录的 changelog.md（Vite `?raw`），
// 构建时内联进产物 —— 发布期不再依赖运行时读取文件，改完 changelog 重新构建即可生效。
import changelogMarkdown from '../../../changelog.md?raw';

import type { EnterpriseAuthUser } from '../auth';

/**
 * 平台更新内容：把仓库根目录 `changelog.md` 按 Markdown 渲染出来。
 * 纯静态页面，不请求后端，所有成员（含无数字员工的新账号）都可访问。
 */
export default function PlatformUpdatesPage({
  currentUser,
  onLogout,
}: {
  currentUser?: EnterpriseAuthUser;
  onLogout?: () => void;
} = {}) {
  return (
    <div className="min-h-full box-border px-[48px] pt-[32px] pb-[43px] max-[900px]:px-[16px]">
      <AppHeader
        className="items-center"
        onLogout={onLogout}
        userName={currentUser?.username}
        title="平台更新内容"
        description="平台功能更新、优化与问题修复的完整记录"
      />

      <section className="mt-[20px] rounded-[16px] border-[0.5px] border-[#e3e7f1] bg-white px-[32px] py-[24px] shadow-[0_2px_12px_rgba(30,45,70,0.04)] max-[900px]:px-[18px]">
        <MarkdownMessage content={changelogMarkdown} />
      </section>
    </div>
  );
}
