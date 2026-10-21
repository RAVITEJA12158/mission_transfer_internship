const express = require('express');
const cors = require('cors');
const env = require('./config/env');
const requireApiKey = require('./middleware/auth.middleware');
const { notFound, errorHandler } = require('./middleware/error.middleware');

const app = express();
app.disable('x-powered-by');
if (env.corsOrigins) app.use(cors({ origin: env.corsOrigins }));
app.use(express.json({ limit: '10mb' }));

app.use('/health', require('./routes/health.routes'));
app.use('/api', requireApiKey);
app.use('/api/missions', require('./routes/missions.routes'));
app.use('/api/telemetry', require('./routes/telemetry.routes'));
app.use('/api/logs', require('./routes/logs.routes'));

app.use(notFound);
app.use(errorHandler);

module.exports = app;
