/**
 * Minimal YouTrack REST helper for creating Customer Support (CS) tickets.
 *
 * Enum custom fields (Channel, Type, Replied) are set via the `customFields`
 * array using the `SingleEnumIssueCustomField` projection — this is the
 * REST-API equivalent of the workflow `ctx.Field.Value` pattern and is the
 * reliable way to set enum values without the Java cast error noted in the
 * helpdesk docs.
 */

/**
 * Create a YouTrack issue.
 *
 * @param {object} opts
 * @param {string} opts.baseUrl   e.g. https://myrealprofit.youtrack.cloud
 * @param {string} opts.token     YouTrack permanent token (Bearer)
 * @param {string} opts.projectId Project shortName or internal id (e.g. "CS")
 * @param {string} opts.summary   Ticket title
 * @param {string} opts.description Ticket body (already includes Source header)
 * @param {string} [opts.channel] Channel enum value name (e.g. "Slack")
 * @param {string} [opts.type]    Type enum value name (e.g. "Task")
 * @param {string} [opts.replied] Replied enum value name (e.g. "Not Replied")
 * @param {string} [opts.customerEmail] Customer Email field value
 * @returns {Promise<{id: string, idReadable: string}>}
 */
export async function createYouTrackIssue(opts) {
  const {
    baseUrl,
    token,
    projectId,
    summary,
    description,
    channel,
    type,
    replied,
    customerEmail,
  } = opts;

  const customFields = [];
  if (channel) customFields.push(enumField('Channel', channel));
  if (type) customFields.push(enumField('Type', type));
  if (replied) customFields.push(enumField('Replied', replied));
  if (customerEmail) {
    customFields.push({
      name: 'Customer Email',
      $type: 'SimpleIssueCustomField',
      value: customerEmail,
    });
  }

  const body = {
    project: { id: projectId },
    summary,
    description,
    customFields,
  };

  const url = `${baseUrl.replace(/\/$/, '')}/api/issues?fields=id,idReadable`;
  const res = await fetch(url, {
    method: 'POST',
    headers: {
      Authorization: `Bearer ${token}`,
      'Content-Type': 'application/json',
      Accept: 'application/json',
    },
    body: JSON.stringify(body),
  });

  if (!res.ok) {
    const text = await res.text();
    throw new Error(`YouTrack create issue failed (${res.status}): ${text}`);
  }
  return res.json();
}

function enumField(name, valueName) {
  return {
    name,
    $type: 'SingleEnumIssueCustomField',
    value: { name: valueName },
  };
}

/**
 * Replace an existing issue's description.
 *
 * Used to keep a ticket in step with a conversation that is still going: the
 * ticket is created the moment Fin hands over, so the SLA clock starts when
 * the customer asked for help rather than when they stopped typing, and the
 * body is rewritten as more of the conversation arrives.
 *
 * YouTrack updates issues with POST, not PATCH.
 */
export async function updateYouTrackIssue({ baseUrl, token, issueId, summary, description }) {
  const body = {};
  if (summary) body.summary = summary;
  if (description) body.description = description;

  const url = `${baseUrl.replace(/\/$/, '')}/api/issues/${issueId}?fields=id,idReadable`;
  const res = await fetch(url, {
    method: 'POST',
    headers: {
      Authorization: `Bearer ${token}`,
      'Content-Type': 'application/json',
      Accept: 'application/json',
    },
    body: JSON.stringify(body),
  });

  if (!res.ok) {
    throw new Error(`YouTrack update failed (${res.status}): ${await res.text()}`);
  }
  return res.json();
}
