import StaffdeckIcon from '@/components/StaffdeckIcon';
import type { KnowledgeCitation } from '@/types';

import {
  CHAT_CITATION_CHIP_CLASS,
  CHAT_CITATION_COUNT_CLASS,
  CHAT_CITATION_HEADING_CLASS,
  CHAT_CITATION_INDEX_CLASS,
  CHAT_CITATION_LIST_CLASS,
  CHAT_CITATION_TITLE_CLASS,
  CHAT_CITATIONS_CLASS,
} from '../chatPageStyles';
import { citationDisplayTitle } from '../chatHelpers';

type KnowledgeCitationListProps = {
  citations: KnowledgeCitation[];
  onOpen: (citation: KnowledgeCitation) => void;
};

export default function KnowledgeCitationList({
  citations,
  onOpen,
}: KnowledgeCitationListProps) {
  if (citations.length === 0) return null;

  const hasWeb = citations.some((citation) => citation.source_type === 'web');

  return (
    <div className={CHAT_CITATIONS_CLASS} aria-label={hasWeb ? '知识来源与互联网来源' : '知识引用'}>
      <div className={CHAT_CITATION_HEADING_CLASS}>
        <StaffdeckIcon name={hasWeb ? 'globe' : 'file'} size={14} />
        <span>{hasWeb ? '来源' : '知识来源'}</span>
      </div>
      <div className={CHAT_CITATION_LIST_CLASS}>
        {citations.map((citation) => {
          const isWeb = citation.source_type === 'web' && Boolean(citation.url);
          return (
            <button
              key={citation.id}
              type="button"
              className={CHAT_CITATION_CHIP_CLASS}
              onClick={() => {
                // 互联网来源直接开原文；企业知识仍走引用详情（本地分块没有网页可跳）
                if (isWeb && citation.url) {
                  window.open(citation.url, '_blank', 'noopener,noreferrer');
                  return;
                }
                onOpen(citation);
              }}
              title={isWeb ? citation.url : undefined}
            >
              <span className={CHAT_CITATION_INDEX_CLASS} data-i18n-ignore>
                {(citation.labels?.length ? citation.labels : [citation.label || citation.id]).join('')}
              </span>
              <span className={CHAT_CITATION_TITLE_CLASS} data-i18n-ignore>
                {citationDisplayTitle(citation)}
              </span>
              {isWeb ? (
                <span className={CHAT_CITATION_COUNT_CLASS} data-i18n-ignore>
                  {citation.site || '网页'}
                </span>
              ) : citation.mergedCount && citation.mergedCount > 1 ? (
                <span className={CHAT_CITATION_COUNT_CLASS} data-i18n-ignore>
                  ×{citation.mergedCount}
                </span>
              ) : null}
            </button>
          );
        })}
      </div>
    </div>
  );
}
