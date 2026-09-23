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

/**
 * Set an enum custom field on an existing issue.
 *
 * Same SingleEnumIssueCustomField projection as creation — anything else hits
 * the Java cast error noted at the top of this file.
 */
export async function setYouTrackEnumField({ baseUrl, token, issueId, field, value }) {
  const url = `${baseUrl.replace(/\/$/, '')}/api/issues/${issueId}?fields=id`;
  const res = await fetch(url, {
    method: 'POST',
    headers: {
      Authorization: `Bearer ${token}`,
      'Content-Type': 'application/json',
      Accept: 'application/json',
    },
    body: JSON.stringify({ customFields: [enumField(field, value)] }),
  });
  if (!res.ok) {
    throw new Error(`YouTrack set ${field} failed (${res.status}): ${await res.text()}`);
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
 * Add a comment to an issue.
 *
 * Used for what the customer says AFTER the ticket exists. Comments preserve
 * chronology, whereas rewriting the description quietly edits history.
 */
export async function addYouTrackComment({ baseUrl, token, issueId, text }) {
  const url = `${baseUrl.replace(/\/$/, '')}/api/issues/${issueId}/comments?fields=id`;
  const res = await fetch(url, {
    method: 'POST',
    headers: {
      Authorization: `Bearer ${token}`,
      'Content-Type': 'application/json',
      Accept: 'application/json',
    },
    body: JSON.stringify({ text }),
  });
  if (!res.ok) {
    throw new Error(`YouTrack comment failed (${res.status}): ${await res.text()}`);
  }
  return res.json();
}

/**
 * Copy a file into YouTrack as a real attachment.
 *
 * The file is fetched from wherever it lives and re-uploaded, rather than
 * linked, so it survives the source URL expiring and is readable by agents who
 * have no seat on the system it came from.
 *
 * Content-Type is deliberately NOT set — FormData must choose its own
 * multipart boundary, and setting the header by hand breaks the upload.
 */
export async function attachToYouTrackIssue({ baseUrl, token, issueId, name, url, authHeader }) {
  const src = await fetch(url, authHeader ? { headers: { Authorization: authHeader } } : {});
  if (!src.ok) {
    throw new Error(`could not download ${name} (${src.status})`);
  }

  const form = new FormData();
  form.append('file', await src.blob(), name || 'attachment');

  const dest = `${baseUrl.replace(/\/$/, '')}/api/issues/${issueId}/attachments?fields=id,name`;
  const res = await fetch(dest, {
    method: 'POST',
    headers: { Authorization: `Bearer ${token}` },
    body: form,
  });
  if (!res.ok) {
    throw new Error(`YouTrack attach failed (${res.status}): ${await res.text()}`);
  }
  return res.json();
}

/**
 * The issue's recent comments, with enough of each one's visibility to judge
 * who may read it.
 *
 * Deliberately a LIST rather than a lookup by id. The workflow reports that
 * something changed; asking it *which* comment means trusting that a workflow
 * comment id matches a REST comment id, which is not documented either way.
 * Reading the issue's own comments needs only the issue id — the one
 * identifier that is already proven to work — and makes the relay idempotent
 * and self-healing: a firing that arrives late, twice, or batched still lands
 * every comment exactly once.
 */
export async function listYouTrackComments({ baseUrl, token, issueId, limit = 20 }) {
  const fields =
    'id,text,created,author(fullName,login),' +
    'visibility($type,permittedGroups(id,name),permittedUsers(id)),' +
    'attachments(id,name,url,mimeType,size)';
  const url =
    `${baseUrl.replace(/\/$/, '')}/api/issues/${issueId}/comments` +
    `?fields=${fields}&$top=${limit}`;
  const res = await fetch(url, {
    headers: { Authorization: `Bearer ${token}`, Accept: 'application/json' },
  });
  if (!res.ok) {
    throw new Error(`YouTrack read comments failed (${res.status}): ${await res.text()}`);
  }
  const comments = await res.json();
  return Array.isArray(comments) ? comments : [];
}

/**
 * Is this comment visible to the customer?
 *
 * FAILS CLOSED. A comment counts as public only when YouTrack positively says
 * it is unrestricted — no visibility object at all, or UnlimitedVisibility with
 * nothing listed. Every other shape, including one we do not recognise, is
 * treated as internal. The cost of a false negative is a reply the agent has
 * to send again; the cost of a false positive is an internal note about a
 * customer, delivered to that customer.
 */
export function isPublicComment(comment) {
  const visibility = comment?.visibility;
  if (!visibility) return true;

  const type = visibility.$type;
  const groups = visibility.permittedGroups || [];
  const users = visibility.permittedUsers || [];

  if (type === 'UnlimitedVisibility') return true;
  if (!type && !groups.length && !users.length) return true;
  return false;
}
