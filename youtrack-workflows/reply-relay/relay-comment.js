/**
 * Send an agent's answer from a CS ticket back to the customer.
 *
 * A PUBLIC comment reaches them; an INTERNAL one never leaves YouTrack. That
 * is the "Internal, visible to Customer Support - Helpdesk Team" toggle an
 * agent already uses on a Gmail ticket, so one habit covers all three
 * channels. Gmail needs no help from this rule — YouTrack mails the reporter
 * itself — so only Slack and Intercom are routed here.
 *
 * This rule deliberately does NOT decide what is public. It reports only which
 * ticket changed; the worker reads the comment back from YouTrack's REST API
 * and sends it only if that confirms it is unrestricted. Comment visibility is
 * REST-side state, and a rule judging it here could publish an internal note
 * to a customer if it judged wrong. The workers fail closed instead.
 *
 * INSTALL
 *   1. YouTrack → Administration → Workflows → the existing reply relay rule,
 *      or New workflow from this file, attached to project CS.
 *   2. Fill in the three variables below. Each WORKER_URL is that worker's
 *      address plus /youtrack/comment. SHARED_SECRET must match the
 *      YOUTRACK_WEBHOOK_SECRET set on BOTH workers.
 *
 * The workers check the secret and ignore anything without it, so these URLs
 * being known does not let anyone post to your customers.
 */

var entities = require('@jetbrains/youtrack-scripting-api/entities');
var http = require('@jetbrains/youtrack-scripting-api/http');

var SLACK_WORKER_URL = 'https://slack-youtrack.filip-1f6.workers.dev/youtrack/comment';
var INTERCOM_WORKER_URL = 'https://intercom-youtrack.filip-1f6.workers.dev/youtrack/comment';
var SHARED_SECRET = 'PASTE-THE-SAME-SECRET-HERE';

exports.rule = entities.Issue.onChange({
  title: 'Relay public comments to the customer',

  guard: function (ctx) {
    if (!ctx.issue.comments.added.isNotEmpty()) return false;
    var channel = ctx.issue.fields.Channel;
    if (channel === null) return false;
    return channel.name === 'Slack' || channel.name === 'Intercom';
  },

  action: function (ctx) {
    var issue = ctx.issue;
    var channel = issue.fields.Channel.name;
    var url = channel === 'Slack' ? SLACK_WORKER_URL : INTERCOM_WORKER_URL;

    // One call per change, not per comment: the worker reads the ticket's
    // recent comments itself and sends whichever it has not sent before. That
    // keeps this rule free of any assumption about comment ids, and makes a
    // firing that arrives late or twice harmless.
    var connection = new http.Connection(url);
    connection.addHeader('Content-Type', 'application/json');
    connection.addHeader('X-Helpdesk-Secret', SHARED_SECRET);
    connection.postSync('', [], JSON.stringify({ issueId: issue.id }));

    // Deliberately silent on failure. Writing the problem back as a comment
    // would re-fire this rule, fail again, and comment again — unbounded while
    // a worker is unreachable. Failures show in the worker logs and in
    // YouTrack's own workflow error log.
  },

  requirements: {
    Channel: {
      type: entities.EnumField.fieldType,
      name: 'Channel',
      Slack: { name: 'Slack' },
      Intercom: { name: 'Intercom' }
    }
  }
});
