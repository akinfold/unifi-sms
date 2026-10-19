'use strict';

const stableTag = /^v(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$/;
const targetTag = /^v(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?$/;

function compare(left, right) {
  for (let index = 0; index < 3; index += 1) {
    const difference = BigInt(left[index + 1]) - BigInt(right[index + 1]);
    if (difference !== 0n) return difference < 0n ? -1 : 1;
  }
  return 0;
}

function previousRelease(releases, target = '') {
  const ceiling = target ? targetTag.exec(target) : null;
  if (target && (!ceiling || target.length > 129 ||
      ceiling[4]?.split('.').some(part => /^0\d+$/.test(part)))) {
    throw new Error('Invalid target release tag.');
  }
  let selected = null;
  let selectedVersion = null;
  for (const release of releases) {
    const version = stableTag.exec(release.tag_name);
    if (!version || release.draft || release.prerelease || !release.published_at) continue;
    if (ceiling && compare(version, ceiling) >= 0) continue;
    if (!selectedVersion || compare(version, selectedVersion) > 0) {
      selected = release;
      selectedVersion = version;
    }
  }
  return selected;
}

module.exports = {previousRelease};
