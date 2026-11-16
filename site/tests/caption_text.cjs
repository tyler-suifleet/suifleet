// Reverse WebVTT presentation wrapping, including textwrap breaks after hyphens.
// A real space after a hyphen stays a space, so misspelled compounds still fail.
module.exports = text => text.replace(/(\S)-\r?\n(?=\S)/g, '$1-').replace(/\s+/g, ' ').trim();
