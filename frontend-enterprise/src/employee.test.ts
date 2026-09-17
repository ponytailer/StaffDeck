import { describe, expect, it } from 'vitest';

import {
  EMPLOYEE_AVATAR_PRESETS,
  EMPLOYEE_TEMPLATES,
  creatorNameFromMetadata,
  employeeAvatarImage,
  employeeDisplayNameWithCreator,
  employeeMetadataFromTemplate,
  resourceCreatorName,
} from './employee';

const EXPANDED_EMPLOYEES = [
  ['sales-advisor', 'sales-handshake', '销售', '客户拓展顾问', 'staffdeck-avatar-sales.png'],
  ['marketing-planner', 'marketing-spark', '市场', '市场内容策划', 'staffdeck-avatar-marketing.png'],
  ['procurement-coordinator', 'procurement-check', '采购', '采购协同专员', 'staffdeck-avatar-procurement.png'],
  ['project-manager', 'project-board', '项目管理', '项目推进经理', 'staffdeck-avatar-project.png'],
  ['data-analyst', 'data-insight', '数据分析', '经营分析师', 'staffdeck-avatar-data.png'],
] as const;

describe('expanded employee presets', () => {
  it.each(EXPANDED_EMPLOYEES)(
    'registers %s with its own avatar and template metadata',
    (roleKey, avatarPreset, categoryName, roleName, avatarFilename) => {
      const preset = EMPLOYEE_AVATAR_PRESETS.find((item) => item.key === avatarPreset);
      const template = EMPLOYEE_TEMPLATES.find((item) => item.key === roleKey);
      const metadata = employeeMetadataFromTemplate(roleKey);
      const avatarImage = employeeAvatarImage({
        avatarKind: 'preset',
        avatarImage: '',
        avatarPreset,
      });

      expect(preset?.label).toContain(categoryName);
      expect(template).toMatchObject({ roleName, avatarPreset });
      expect(metadata).toMatchObject({
        role_key: roleKey,
        role_name: roleName,
        avatar_kind: 'preset',
        avatar_preset: avatarPreset,
      });
      expect(avatarImage).toContain(avatarFilename);
    },
  );

  it('uses a distinct illustration for each expanded employee', () => {
    const images = EXPANDED_EMPLOYEES.map(([, avatarPreset]) => employeeAvatarImage({
      avatarKind: 'preset',
      avatarImage: '',
      avatarPreset,
    }));

    expect(new Set(images)).toHaveLength(EXPANDED_EMPLOYEES.length);
  });
});

// 线上真实 metadata 形状（后端 user_creator_metadata 写入）：
// creator_name/created_by/created_by_username 存**用户名**，显示名另存 display_name。
const PLAZA_METADATA = {
  scope: 'open_gallery',
  creator_name: 'chenyi.cq',
  created_by: 'chenyi.cq',
  created_by_username: 'chenyi.cq',
  created_by_display_name: '陈怡',
  owner_username: 'chenyi.cq',
  owner_display_name: '陈怡',
  owner_user_id: 'user_5faedb0287a547c8',
};

describe('creator display name', () => {
  it('prefers the display name over the stamped username', () => {
    expect(creatorNameFromMetadata(PLAZA_METADATA)).toBe('陈怡');
    expect(resourceCreatorName({ metadata: PLAZA_METADATA })).toBe('陈怡');
  });

  it('falls back to the username when no display name was stamped', () => {
    expect(creatorNameFromMetadata({ creator_name: 'user_demo' })).toBe('user_demo');
    expect(creatorNameFromMetadata({ created_by_username: 'user_demo' })).toBe('user_demo');
  });

  it('uses the display name in the plaza card title suffix', () => {
    const agent = {
      id: 'agent-1',
      name: 'AI G.O',
      is_overall: false,
      metadata: { ...PLAZA_METADATA, created_by_display_name: '黄松', owner_display_name: '黄松' },
    } as never;
    expect(employeeDisplayNameWithCreator(agent)).toBe('AI G.O @黄松');
  });

  it('keeps the caller-provided fallback when nothing is stamped', () => {
    expect(creatorNameFromMetadata({}, '未知')).toBe('未知');
    expect(creatorNameFromMetadata(null, '未知')).toBe('未知');
  });
});
