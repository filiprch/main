/**
 * Send an agent's answer from a CS ticket back into its Slack thread.
 *
 * A PUBLIC comment reaches the customer; an INTERNAL one never leaves
 * YouTrack. That is the same "Internal, visible to Customer Support - Helpdesk
 * Team" toggle an agent already uses on a Gmail ticket, so there is one habit
 * across every channel instead of a per-channel convention to remember.
 *
 * This rule deliberately does NOT decide what is public. It reports only which
 * comment changed; the worker reads the comment back from YouTrack's REST API
 * and relays it only if that confirms it is unrestricted. Comment visibility
 * is REST-side state, and a rule that judged it here could relay an internal
 * note to a customer if it judged wrong. The worker fails closed instead.
 *
 * INSTALL
 *   1. YouTrack → Administration → Workflows → New workflow → from this file,
 *      then attach it to project CS.
 *   2. Set the two variables below. WORKER_URL is your deployed worker plus
 *      /youtrack/comment. SHARED_SECRET must match the worker's
 *      YOUTRACK_WEBHOOK_SECRET exactly (npx wrangler secret put).
 *
 * The worker checks the secret and ignores anything without it, so this URL
 * being known does not let anyone post into your Slack.
 */

var entities = require('@jetbrains/youtrack-scripting-api/entities');
var http = require('@jetbrains/youtrack-scripting-api/http');

var WORKER_URL = 'https://slack-youtrack.filip-1f6.workers.dev/youtrack/comment';
var SHARED_SECRET = 'PASTE-THE-SAME-SECRET-HERE';

exports.rule = entities.Issue.onChange({
  title: 'Relay public comments into Slack',

  // Only fires when a comment was added, and only on tickets that came from
  // Slack — a Gmail or Intercom ticket has no Slack thread to answer into.
  guard: function (ctx) {
    return (
      ctx.issue.comments.added.isNotEmpty() &&
      ctx.issue.fields.Channel !== null &&
      ctx.issue.fields.Channel.name === 'Slack'
    );
  },

  action: function (ctx) {
    var issue = ctx.issue;

    issue.comments.added.forEach(function (comment) {
      var connection = new http.Connection(WORKER_URL);
      connection.addHeader('Content-Type', 'application/json');
      connection.addHeader('X-Helpdesk-Secret', SHARED_SECRET);

      var response = connection.postSync(
        '',
        [],
        JSON.stringify({
          issueId: issue.id,
          commentId: comment.id
        })
      );

      // Deliberately silent on failure. Writing the problem back as a comment
      // would be more helpful, but this rule fires on added comments — so a
      // failure comment re-fires it, fails again, and comments again. When the
      // worker is unreachable, that is an unbounded loop. Failures show in the
      // worker's log and in YouTrack's own workflow error log instead.
    });
  },

  requirements: {
    Channel: {
      type: entities.EnumField.fieldType,
      name: 'Channel',
      Slack: { name: 'Slack' }
    }
  }
});
