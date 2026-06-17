const express = require('express');
const { Pool } = require('pg');
const cors = require('cors');
const path = require('path');

const app = express();
app.use(cors());
app.use(express.json());
app.use(express.static(path.join(__dirname, 'public')));

const pool = new Pool({
  host: '136.112.125.16',
  database: 'postgres',
  user: 'filip_read_only',
  password: ').NExQ,N0{zfpadi',
  port: 5432,
  ssl: { rejectUnauthorized: false },
  connectionTimeoutMillis: 10000,
});

const QUERIES = {
  stuck_sqp_reports: {
    label: 'Step 1: Stuck SQP Reports',
    description: 'Shows sellers with stuck SQP_BY_ASIN reports (not DONE/UNABLE_TO_GENERATE/etc)',
    sql: `SELECT
    ari.amazon_selling_partner_id,
    asp.name AS seller_name,
    COUNT(*)                                             AS stuck_reports_total,
    COUNT(*) FILTER (WHERE ari.amazon_region_id = 1)    AS region_1_count,
    COUNT(*) FILTER (WHERE ari.amazon_region_id = 2)    AS region_2_count,
    COUNT(*) FILTER (WHERE ari.amazon_region_id = 3)    AS region_3_count,
    asat.refresh_token
FROM amazon_report_info ari
JOIN amazon_selling_partner asp
    ON asp.id = ari.amazon_selling_partner_id
JOIN amazon_selling_api_token asat
    ON asat.amazon_selling_partner_id = ari.amazon_selling_partner_id
WHERE ari.report_type = 'SQP_BY_ASIN'
  and status not in ('DONE','UNABLE_TO_GENERATE','REPORT_GENERATED_BY_AMAZON','DOWNLOADING_REPORT')
  AND ari.amazon_requested_report_id IS NOT NULL
GROUP BY
    ari.amazon_selling_partner_id,
    asp.name,
    asat.refresh_token
ORDER BY stuck_reports_total DESC`,
  },
};

app.get('/api/queries', (req, res) => {
  const list = Object.entries(QUERIES).map(([key, val]) => ({
    key,
    label: val.label,
    description: val.description,
  }));
  res.json(list);
});

app.post('/api/query/:key', async (req, res) => {
  const query = QUERIES[req.params.key];
  if (!query) return res.status(404).json({ error: 'Query not found' });

  const start = Date.now();
  try {
    const result = await pool.query(query.sql);
    res.json({
      rows: result.rows,
      rowCount: result.rowCount,
      duration_ms: Date.now() - start,
    });
  } catch (err) {
    res.status(500).json({ error: err.message });
  }
});

app.post('/api/custom-query', async (req, res) => {
  const { sql } = req.body;
  if (!sql) return res.status(400).json({ error: 'sql is required' });

  const start = Date.now();
  try {
    const result = await pool.query(sql);
    res.json({
      rows: result.rows,
      rowCount: result.rowCount,
      duration_ms: Date.now() - start,
    });
  } catch (err) {
    res.status(500).json({ error: err.message });
  }
});

const PORT = 3000;
app.listen(PORT, '0.0.0.0', () => {
  console.log(`Dashboard running on http://localhost:${PORT}`);
});
