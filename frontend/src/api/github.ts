/**
 * Update check — disabled. This build is self-contained and does not contact
 * any upstream repository, so there is never a "new version" to announce.
 * The exports are kept so the settings / account panels need no changes.
 */

export interface LatestRelease {
  tagName: string;
  htmlUrl: string;
  publishedAt: string;
}

export async function getLatestRelease(): Promise<LatestRelease | null> {
  return null;
}

export function isNewerVersion(_latest: string, _current: string): boolean {
  return false;
}
