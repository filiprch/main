/**
 * Send an agent's answer from a CS ticket back into its Slack thread.
 *
 * A comment beginning "Reply:" is passed to the slack-youtrack worker, which
 * strips the prefix and posts the rest into the thread the ticket came from.
 * Every other comment stays internal — private is the default, and going
 * public is a deliberate act by whoever writes the comment.
 *
 *   in YouTrack:  Reply: Yes, that is correct.
 *   in Slack:     Lisa: Yes, that is correct.
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
  title: 'Relay "Reply:" comments into Slack',

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
      var text = (comment.text || '').trim();
      if (text.toLowerCase().indexOf('reply:') !== 0) {
        return; // internal note — nothing leaves YouTrack
      }

      var author = comment.author;
      var name = author ? author.fullName || author.login : '';

      var connection = new http.Connection(WORKER_URL);
      connection.addHeader('Content-Type', 'application/json');
      connection.addHeader('X-Helpdesk-Secret', SHARED_SECRET);

      var response = connection.postSync(
        '',
        [],
        JSON.stringify({
          issueId: issue.id,
          commentId: comment.id,
          author: name,
          text: text
        })
      );

      // Say so in the ticket rather than failing quietly: an agent who thinks
      // they have answered a customer and has not is worse off than one who
      // can see it did not go.
      if (!response.isSuccess) {
        issue.addComment(
          'This reply could not be sent to Slack (HTTP ' +
            response.code +
            '). The customer has not seen it.'
        );
      }
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
