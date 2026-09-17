import { describe, expect, it } from 'vitest';

import { ApiError } from '@/api/client';

import {
  GENERAL_SKILL_MISSING_REFERENCES_CODE,
  missingSkillReferencePaths,
} from './general-skill-import-error';

function apiError(status: number, detail: unknown): ApiError {
  return new ApiError(status, JSON.stringify({ detail }), 'Bad Request');
}

describe('missingSkillReferencePaths', () => {
  it('extracts the missing reference paths from the structured detail', () => {
    const error = apiError(400, {
      code: GENERAL_SKILL_MISSING_REFERENCES_CODE,
      message: 'General skill package is missing files referenced by SKILL.md: a.md, b.md',
      paths: ['references/products/aiapp.md', 'references/products/pat.md'],
    });

    expect(missingSkillReferencePaths(error)).toEqual([
      'references/products/aiapp.md',
      'references/products/pat.md',
    ]);
  });

  it('ignores other 400 errors', () => {
    expect(missingSkillReferencePaths(apiError(400, 'General skill slug already exists'))).toEqual([]);
    expect(
      missingSkillReferencePaths(
        apiError(400, { code: 'SOMETHING_ELSE', paths: ['references/products/aiapp.md'] }),
      ),
    ).toEqual([]);
  });

  it('ignores non-400 responses and non-ApiError values', () => {
    expect(
      missingSkillReferencePaths(
        apiError(409, { code: GENERAL_SKILL_MISSING_REFERENCES_CODE, paths: ['a.md'] }),
      ),
    ).toEqual([]);
    expect(missingSkillReferencePaths(new Error('boom'))).toEqual([]);
    expect(missingSkillReferencePaths(null)).toEqual([]);
  });

  it('falls back to an empty list when the body is not JSON', () => {
    expect(missingSkillReferencePaths(new ApiError(400, '<html>oops</html>', 'Bad Request'))).toEqual([]);
  });
});
