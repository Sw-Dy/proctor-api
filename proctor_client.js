#!/usr/bin/env node

/**
 * ╔══════════════════════════════════════════════════════════════════╗
 * ║    AI EXAM PROCTOR — Node.js Client Script                      ║
 * ║    Remote control for the Python proctoring service             ║
 * ║    Usage: node proctor_client.js [command] [options]            ║
 * ╚══════════════════════════════════════════════════════════════════╝
 *
 * Installation:
 *   npm install axios chalk table
 *
 * Commands:
 *   node proctor_client.js start              — Start a proctoring session
 *   node proctor_client.js stop               — Stop the current session
 *   node proctor_client.js status             — Show current status
 *   node proctor_client.js stats              — Show session statistics
 *   node proctor_client.js screenshot         — Capture screenshot
 *   node proctor_client.js reset              — Reset violation counters
 *   node proctor_client.js monitor            — Real-time monitoring dashboard
 *   node proctor_client.js help               — Show help
 */

const axios = require('axios');
const chalk = require('chalk');
const Table = require('table').Table;
const fs = require('fs');
const path = require('path');
const io = require('socket.io-client');

// ─────────────────────────────────────────────────────────────────
// CONFIGURATION
// ─────────────────────────────────────────────────────────────────

const API_BASE_URL = process.env.PROCTOR_API || 'http://localhost:8765';
const API_CONFIG = {
    baseURL: API_BASE_URL,
    timeout: 10000,
    headers: {
        'Content-Type': 'application/json',
    }
};

// ─────────────────────────────────────────────────────────────────
// UTILITY FUNCTIONS
// ─────────────────────────────────────────────────────────────────

function log(message, type = 'info') {
    const timestamp = new Date().toLocaleTimeString();
    const prefix = `[${timestamp}]`;
    
    switch (type) {
        case 'success':
            console.log(chalk.green(`${prefix} ✓ ${message}`));
            break;
        case 'error':
            console.log(chalk.red(`${prefix} ✗ ${message}`));
            break;
        case 'warning':
            console.log(chalk.yellow(`${prefix} ⚠ ${message}`));
            break;
        case 'info':
            console.log(chalk.blue(`${prefix} ℹ ${message}`));
            break;
        case 'debug':
            if (process.env.DEBUG) {
                console.log(chalk.gray(`${prefix} 🔍 ${message}`));
            }
            break;
        default:
            console.log(`${prefix} ${message}`);
    }
}

function printHeader(title) {
    const width = 64;
    const padding = Math.floor((width - title.length - 2) / 2);
    console.log();
    console.log(chalk.cyan('═'.repeat(width)));
    console.log(chalk.cyan('║ ' + title.padEnd(width - 3) + '║'));
    console.log(chalk.cyan('═'.repeat(width)));
    console.log();
}

function printHelp() {
    console.log(`
${chalk.bold.cyan('AI EXAM PROCTOR — Node.js Client')}

${chalk.bold('Commands:')}

  ${chalk.green('start')}              Start a new proctoring session
  ${chalk.green('stop')}               Stop the current session
  ${chalk.green('status')}             Display current system status
  ${chalk.green('stats')}              Show detailed session statistics
  ${chalk.green('screenshot')}         Capture a screenshot
  ${chalk.green('reset')}              Reset violation counters
  ${chalk.green('monitor')}            Real-time monitoring dashboard
  ${chalk.green('help')}               Show this help message

${chalk.bold('Environment Variables:')}

  PROCTOR_API     API server URL (default: http://localhost:8765)
  DEBUG           Enable debug logging (set to 1)

${chalk.bold('Examples:')}

  # Start a session and monitor it
  node proctor_client.js start
  node proctor_client.js monitor

  # Get current status
  node proctor_client.js status

  # Connect to remote server
  PROCTOR_API=http://192.168.1.100:8765 node proctor_client.js stats

${chalk.bold('Requirements:')}

  Make sure the Python proctoring service is running:
  python proctor_service.py
    `);
}

// ─────────────────────────────────────────────────────────────────
// API CLIENT
// ─────────────────────────────────────────────────────────────────

class ProctorClient {
    constructor(baseURL) {
        this.client = axios.create({ ...API_CONFIG, baseURL });
        this.socket = null;
    }

    async start() {
        try {
            log('Starting proctoring session...', 'info');
            const response = await this.client.post('/api/start');
            if (response.data.error) {
                throw new Error(response.data.error);
            }
            log(`Session started: ${response.data.session_id}`, 'success');
            return response.data;
        } catch (error) {
            log(`Failed to start session: ${error.message}`, 'error');
            throw error;
        }
    }

    async stop() {
        try {
            log('Stopping proctoring session...', 'info');
            const response = await this.client.post('/api/stop');
            if (response.data.error) {
                throw new Error(response.data.error);
            }
            log('Session stopped', 'success');
            return response.data;
        } catch (error) {
            log(`Failed to stop session: ${error.message}`, 'error');
            throw error;
        }
    }

    async getStatus() {
        try {
            const response = await this.client.get('/api/status');
            return response.data;
        } catch (error) {
            log(`Failed to get status: ${error.message}`, 'error');
            throw error;
        }
    }

    async getStats() {
        try {
            const response = await this.client.get('/api/stats');
            if (response.data.error) {
                throw new Error(response.data.error);
            }
            return response.data;
        } catch (error) {
            log(`Failed to get stats: ${error.message}`, 'error');
            throw error;
        }
    }

    async performAction(action) {
        try {
            const response = await this.client.post('/api/action', { action });
            if (response.data.error) {
                throw new Error(response.data.error);
            }
            return response.data;
        } catch (error) {
            log(`Action failed: ${error.message}`, 'error');
            throw error;
        }
    }

    async getConfig() {
        try {
            const response = await this.client.get('/api/config');
            return response.data;
        } catch (error) {
            log(`Failed to get config: ${error.message}`, 'error');
            throw error;
        }
    }

    connectWebSocket() {
        return new Promise((resolve, reject) => {
            try {
                this.socket = io(API_BASE_URL, {
                    reconnection: true,
                    reconnectionDelay: 1000,
                    reconnectionDelayMax: 5000,
                });

                this.socket.on('connect', () => {
                    log('WebSocket connected', 'success');
                    resolve(this.socket);
                });

                this.socket.on('connect_error', (error) => {
                    log(`WebSocket error: ${error}`, 'error');
                    reject(error);
                });

                this.socket.on('disconnect', () => {
                    log('WebSocket disconnected', 'warning');
                });

            } catch (error) {
                reject(error);
            }
        });
    }

    disconnectWebSocket() {
        if (this.socket) {
            this.socket.disconnect();
        }
    }
}

// ─────────────────────────────────────────────────────────────────
// DISPLAY FUNCTIONS
// ─────────────────────────────────────────────────────────────────

function displayStatus(status) {
    console.log();
    console.log(chalk.bold('═ SYSTEM STATUS ═'));
    
    const table = new Table({
        colWidth: [25, 35],
        style: { head: [], border: ['cyan'] }
    });

    const statusColor = status.status === 'running' 
        ? chalk.green 
        : status.status === 'stopped' 
        ? chalk.red 
        : chalk.gray;

    table.push(
        [chalk.bold('Status'), statusColor(status.status.toUpperCase())],
        [chalk.bold('Session ID'), chalk.cyan(status.session_id || '—')],
        [chalk.bold('Uptime'), chalk.yellow(formatSeconds(status.uptime_sec))],
        [chalk.bold('Frames Processed'), chalk.cyan(status.frame_count)],
        [chalk.bold('Total Violations'), status.violations > 5 
            ? chalk.red(status.violations) 
            : status.violations > 0 
            ? chalk.yellow(status.violations) 
            : chalk.green(status.violations)],
        [chalk.bold('Suspicious Score'), 
            status.suspicious_score > 60 
            ? chalk.red(`${status.suspicious_score.toFixed(1)}/100`)
            : status.suspicious_score > 25
            ? chalk.yellow(`${status.suspicious_score.toFixed(1)}/100`)
            : chalk.green(`${status.suspicious_score.toFixed(1)}/100`)],
        [chalk.bold('Face Detected'), status.face_detected ? chalk.green('✓') : chalk.red('✗')],
        [chalk.bold('Gaze'), status.gaze_away ? chalk.yellow('AWAY') : chalk.green('OK')],
        [chalk.bold('Head'), status.head_turned ? chalk.yellow('TURNED') : chalk.green('NORMAL')],
        [chalk.bold('Speaking'), status.mouth_open ? chalk.yellow('YES') : chalk.green('NO')],
        [chalk.bold('Audio (RMS)'), chalk.cyan(Math.round(status.audio_rms))]
    );

    console.log(table.toString());
    console.log();
}

function displayStats(stats) {
    console.log();
    console.log(chalk.bold('═ SESSION STATISTICS ═'));
    
    const table = new Table({
        colWidth: [25, 35],
        style: { head: [], border: ['cyan'] }
    });

    table.push(
        [chalk.bold('Session ID'), chalk.cyan(stats.session_id || '—')],
        [chalk.bold('Start Time'), chalk.yellow(stats.start_time || '—')],
        [chalk.bold('Duration'), chalk.yellow(formatSeconds(stats.uptime_sec))],
        [chalk.bold('Frames Processed'), chalk.cyan(stats.frame_count)],
        [chalk.bold('Total Violations'), chalk.red(stats.total_violations)],
        [chalk.bold('Avg Suspicious Score'), chalk.yellow(`${stats.avg_score}/100`)],
        [chalk.bold('Max Suspicious Score'), chalk.red(`${stats.max_score}/100`)],
        [chalk.bold('Total Blinks'), chalk.cyan(stats.total_blinks)]
    );

    console.log(table.toString());

    if (stats.violation_counts && Object.keys(stats.violation_counts).length > 0) {
        console.log();
        console.log(chalk.bold('Violation Breakdown:'));
        const violationTable = new Table({
            colWidth: [25, 15],
            style: { head: [], border: ['cyan'] }
        });

        for (const [type, count] of Object.entries(stats.violation_counts)) {
            violationTable.push([chalk.yellow(type), chalk.red(count)]);
        }

        console.log(violationTable.toString());
    }

    console.log();
}

function formatSeconds(seconds) {
    const hours = Math.floor(seconds / 3600);
    const minutes = Math.floor((seconds % 3600) / 60);
    const secs = Math.floor(seconds % 60);
    
    if (hours > 0) {
        return `${hours}h ${minutes}m ${secs}s`;
    } else if (minutes > 0) {
        return `${minutes}m ${secs}s`;
    } else {
        return `${secs}s`;
    }
}

// ─────────────────────────────────────────────────────────────────
// MONITORING MODE
// ─────────────────────────────────────────────────────────────────

async function monitorSession(client) {
    printHeader('REAL-TIME MONITORING');
    
    try {
        await client.connectWebSocket();
        
        log('Monitoring session... Press Ctrl+C to exit', 'info');
        console.log();

        let lastUpdateTime = Date.now();

        client.socket.on('state_update', (state) => {
            const now = Date.now();
            if (now - lastUpdateTime > 500) {  // Update every 500ms
                process.stdout.write('\x1Bc');  // Clear screen
                
                displayStatus(state);

                // Alert for critical events
                if (state.violations > 5) {
                    console.log(chalk.bgRed.bold(' ⚠ HIGH VIOLATIONS '));
                }
                if (state.suspicious_score > 60) {
                    console.log(chalk.bgRed.bold(' ⚠ HIGH SUSPICIOUS SCORE '));
                }
                if (!state.face_detected) {
                    console.log(chalk.bgYellow.bold(' ⚠ NO FACE DETECTED '));
                }

                lastUpdateTime = now;
            }
        });

        // Keep process alive
        await new Promise(() => {});

    } catch (error) {
        log(`Monitoring failed: ${error.message}`, 'error');
    }
}

// ─────────────────────────────────────────────────────────────────
// MAIN
// ─────────────────────────────────────────────────────────────────

async function main() {
    const command = process.argv[2] || 'help';
    const client = new ProctorClient(API_BASE_URL);

    try {
        switch (command.toLowerCase()) {
            case 'start':
                printHeader('START SESSION');
                await client.start();
                break;

            case 'stop':
                printHeader('STOP SESSION');
                await client.stop();
                break;

            case 'status':
                printHeader('STATUS CHECK');
                const status = await client.getStatus();
                displayStatus(status);
                break;

            case 'stats':
                printHeader('SESSION STATISTICS');
                const stats = await client.getStats();
                displayStats(stats);
                break;

            case 'screenshot':
                printHeader('SCREENSHOT');
                const screenshot = await client.performAction('screenshot');
                log(`Screenshot saved: ${screenshot.path}`, 'success');
                break;

            case 'reset':
                printHeader('RESET COUNTERS');
                await client.performAction('reset');
                log('Violation counters reset', 'success');
                break;

            case 'monitor':
                await monitorSession(client);
                break;

            case 'help':
            default:
                printHelp();
                break;
        }
    } catch (error) {
        log(`Command failed: ${error.message}`, 'error');
        process.exit(1);
    }

    client.disconnectWebSocket();
}

// Handle graceful shutdown
process.on('SIGINT', () => {
    console.log();
    log('Shutting down...', 'warning');
    process.exit(0);
});

main();
