const assert = require('node:assert/strict');
const {test} = require('node:test');
const {previousRelease} = require('../.github/scripts/previous-release.cjs');

const published = (tag_name, overrides = {}) => ({
  tag_name, draft: false, prerelease: false, published_at: '2026-01-01T00:00:00Z', ...overrides,
});

test('ordinary CI selects the highest stable version regardless of release order', () => {
  assert.equal(previousRelease([
    published('v0.9.0'), published('v0.10.0'), published('v0.2.0'),
  ]).tag_name, 'v0.10.0');
});

test('rebuilding an older release excludes that release and newer versions', () => {
  assert.equal(previousRelease([
    published('v0.1.2'), published('v0.1.0'), published('v0.1.1'),
  ], 'v0.1.1').tag_name, 'v0.1.0');
});

test('a prerelease uses the previous stable version, excluding its future stable release', () => {
  assert.equal(previousRelease([
    published('v0.1.2'), published('v0.1.3'),
    published('v0.1.3-rc.1', {prerelease: true}),
  ], 'v0.1.3-rc.2').tag_name, 'v0.1.2');
});

test('drafts, prereleases, unpublished releases, and nonstandard tags are ignored', () => {
  assert.equal(previousRelease([
    published('v1.0.0'), published('v2.0.0', {draft: true}),
    published('v3.0.0', {prerelease: true}), published('v4.0.0', {published_at: null}),
    published('v5.0.0-rc.1'), published('v06.0.0'), published('7.0.0'),
    published('v8.0.0+build.1'),
  ]).tag_name, 'v1.0.0');
});

test('a first release has no predecessor', () => {
  assert.equal(previousRelease([published('v0.1.0')], 'v0.1.0'), null);
  assert.equal(previousRelease([]), null);
});

test('version comparison retains precision for large numeric components', () => {
  assert.equal(previousRelease([
    published('v9007199254740992.0.0'), published('v9007199254740993.0.0'),
  ]).tag_name, 'v9007199254740993.0.0');
});

test('invalid target versions fail instead of skipping the upgrade check', () => {
  for (const target of ['latest', 'v01.0.0', 'v1.0.0-01', 'v1.0.0+build.1']) {
    assert.throws(() => previousRelease([], target), /Invalid target/);
  }
});
