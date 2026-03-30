import { useState, useEffect } from 'react'
import { useNavigate } from 'react-router-dom'
import api from '../api'

export default function Dashboard() {
  const [stats, setStats] = useState(null)
  const [jobs, setJobs] = useState([])
  const [loading, setLoading] = useState(true)
  const [applying, setApplying] = useState(null)
  const navigate = useNavigate()
  const name = localStorage.getItem('name')

  useEffect(() => {
    loadData()
  }, [])

  const loadData = async () => {
    try {
      const [statsRes, jobsRes] = await Promise.all([
        api.get('/jobs/stats'),
        api.get('/jobs/?min_score=7&limit=50')
      ])
      setStats(statsRes.data)
      setJobs(jobsRes.data)
    } catch {
      navigate('/login')
    } finally {
      setLoading(false)
    }
  }

  const applyToJob = async (jobId) => {
    setApplying(jobId)
    try {
      await api.post(`/apply/${jobId}?dry_run=true`)
      alert('Application started! Check your terminal to watch it run.')
    } catch (err) {
      alert('Failed to start application')
    } finally {
      setApplying(null)
    }
  }

  const logout = () => {
    localStorage.clear()
    navigate('/login')
  }

  const scoreColor = (score) => {
    if (score >= 9) return { bg: '#dcfce7', text: '#15803d', dot: '#22c55e' }
    if (score >= 7) return { bg: '#fef9c3', text: '#a16207', dot: '#eab308' }
    return { bg: '#f1f5f9', text: '#475569', dot: '#94a3b8' }
  }

  const initials = name ? name.split(' ').map(n => n[0]).join('').toUpperCase().slice(0, 2) : '?'

  if (loading) return (
    <div style={styles.loadingScreen}>
      <div style={styles.loadingSpinner} />
      <p style={styles.loadingText}>Loading your dashboard...</p>
    </div>
  )

  return (
    <div style={styles.page}>
      {/* Navbar */}
      <nav style={styles.navbar}>
        <div style={styles.navLeft}>
          <div style={styles.navLogoMark}>⚡</div>
          <span style={styles.navLogo}>JobBot</span>
        </div>
        <div style={styles.navRight}>
          <div style={styles.navUser}>
            <div style={styles.avatar}>{initials}</div>
            <span style={styles.navName}>Hi, {name}</span>
          </div>
          <button onClick={logout} className="logout-btn" style={styles.logoutBtn}>
            Sign out
          </button>
        </div>
      </nav>

      {/* Page content */}
      <div style={styles.content}>

        {/* Page header */}
        <div className="fade-in" style={styles.pageHeader}>
          <div>
            <h1 style={styles.pageTitle}>Your Dashboard</h1>
            <p style={styles.pageSubtitle}>AI is scanning jobs and applying on your behalf</p>
          </div>
          <button onClick={loadData} style={styles.refreshBtn}>
            ↻ Refresh
          </button>
        </div>

        {/* Stats row */}
        {stats && (
          <div style={styles.statsRow}>
            <div className="stat-card fade-in-1" style={{ ...styles.statCard, borderTopColor: '#9333ea' }}>
              <div style={styles.statIcon}>🔍</div>
              <div style={styles.statNum}>{stats.total_jobs?.toLocaleString()}</div>
              <div style={styles.statLabel}>Jobs found</div>
              <div style={{ ...styles.statBar, background: '#ede9fe' }}>
                <div style={{ ...styles.statBarFill, background: '#9333ea', width: '100%' }} />
              </div>
            </div>
            <div className="stat-card fade-in-2" style={{ ...styles.statCard, borderTopColor: '#8b5cf6' }}>
              <div style={styles.statIcon}>⭐</div>
              <div style={styles.statNum}>{stats.strong_matches}</div>
              <div style={styles.statLabel}>Strong matches</div>
              <div style={{ ...styles.statBar, background: '#ede9fe' }}>
                <div style={{ ...styles.statBarFill, background: '#8b5cf6', width: stats.total_jobs ? `${Math.min(100, (stats.strong_matches / stats.total_jobs) * 100)}%` : '0%' }} />
              </div>
            </div>
            <div className="stat-card fade-in-3" style={{ ...styles.statCard, borderTopColor: '#22c55e' }}>
              <div style={styles.statIcon}>✅</div>
              <div style={{ ...styles.statNum, color: '#15803d' }}>{stats.applied}</div>
              <div style={styles.statLabel}>Applied</div>
              <div style={{ ...styles.statBar, background: '#dcfce7' }}>
                <div style={{ ...styles.statBarFill, background: '#22c55e', width: stats.total_jobs ? `${Math.min(100, (stats.applied / stats.total_jobs) * 100)}%` : '0%' }} />
              </div>
            </div>
            <div className="stat-card fade-in-4" style={{ ...styles.statCard, borderTopColor: '#f59e0b' }}>
              <div style={styles.statIcon}>📊</div>
              <div style={styles.statNum}>{stats.scored}</div>
              <div style={styles.statLabel}>Scored</div>
              <div style={{ ...styles.statBar, background: '#fef9c3' }}>
                <div style={{ ...styles.statBarFill, background: '#f59e0b', width: stats.total_jobs ? `${Math.min(100, (stats.scored / stats.total_jobs) * 100)}%` : '0%' }} />
              </div>
            </div>
          </div>
        )}

        {/* Jobs section */}
        <div className="fade-in-3" style={styles.sectionHeader}>
          <h2 style={styles.sectionTitle}>Top matches</h2>
          <span style={styles.sectionCount}>{jobs.length} jobs</span>
        </div>

        <div style={styles.jobList}>
          {jobs.length === 0 && (
            <div style={styles.emptyState}>
              <div style={styles.emptyIcon}>🎯</div>
              <p style={styles.emptyText}>No jobs found yet. The bot is scanning...</p>
            </div>
          )}
          {jobs.map((job, i) => {
            const sc = scoreColor(job.score)
            const isApplied = job.status === 'applied'
            return (
              <div
                key={job.id}
                className={`job-card fade-in`}
                style={{ ...styles.jobCard, animationDelay: `${i * 0.04}s` }}
              >
                {/* Left accent bar */}
                <div style={{ ...styles.jobAccent, background: sc.dot }} />

                <div style={styles.jobBody}>
                  <div style={styles.jobTop}>
                    <div style={styles.jobInfo}>
                      <div style={styles.jobTitle}>{job.title}</div>
                      <div style={styles.jobMeta}>
                        <span style={styles.jobCompany}>{job.company}</span>
                        {job.location && (
                          <>
                            <span style={styles.metaDot}>·</span>
                            <span style={styles.jobLocation}>📍 {job.location}</span>
                          </>
                        )}
                      </div>
                    </div>

                    <div style={styles.jobBadges}>
                      <div style={{ ...styles.scoreBadge, background: sc.bg, color: sc.text }}>
                        <span style={{ ...styles.scoreDot, background: sc.dot }} />
                        {job.score}/10
                      </div>
                      <div style={{
                        ...styles.statusBadge,
                        background: isApplied ? '#dcfce7' : '#f3e8ff',
                        color: isApplied ? '#15803d' : '#7c3aed',
                      }}>
                        {isApplied ? '✓ Applied' : '⏳ Pending'}
                      </div>
                    </div>
                  </div>

                  <div style={styles.jobActions}>
                    <a href={job.url} target="_blank" rel="noreferrer" style={styles.viewBtn}>
                      View posting ↗
                    </a>
                    <button
                      onClick={() => applyToJob(job.id)}
                      disabled={applying === job.id || isApplied}
                      className="apply-btn"
                      style={{
                        ...styles.applyBtn,
                        background: isApplied
                          ? '#f1f5f9'
                          : 'linear-gradient(135deg, #9333ea, #7c3aed)',
                        color: isApplied ? '#94a3b8' : '#fff',
                        cursor: isApplied ? 'default' : 'pointer',
                        boxShadow: isApplied ? 'none' : '0 3px 10px rgba(124,58,237,0.3)',
                      }}
                    >
                      {applying === job.id
                        ? <span style={styles.btnLoadingInner}><span style={styles.btnSpinner} /> Starting...</span>
                        : isApplied
                          ? '✓ Applied'
                          : '⚡ Auto Apply'}
                    </button>
                  </div>
                </div>
              </div>
            )
          })}
        </div>
      </div>
    </div>
  )
}

const styles = {
  page: {
    minHeight: '100vh',
    background: 'linear-gradient(180deg, #faf5ff 0%, #f3e8ff 30%, #faf5ff 100%)',
    fontFamily: "'Inter', sans-serif",
  },
  /* Loading screen */
  loadingScreen: {
    minHeight: '100vh',
    display: 'flex', flexDirection: 'column',
    alignItems: 'center', justifyContent: 'center',
    background: 'linear-gradient(135deg, #faf5ff, #ede9fe)',
    gap: '16px',
  },
  loadingSpinner: {
    width: '44px', height: '44px',
    border: '3px solid #e9d5ff',
    borderTopColor: '#9333ea',
    borderRadius: '50%',
    animation: 'spin 0.8s linear infinite',
  },
  loadingText: { fontSize: '15px', color: '#7c3aed', fontWeight: '500' },
  /* Navbar */
  navbar: {
    background: 'rgba(255,255,255,0.9)',
    backdropFilter: 'blur(12px)',
    borderBottom: '1px solid #e9d5ff',
    padding: '0 32px',
    height: '64px',
    display: 'flex', alignItems: 'center', justifyContent: 'space-between',
    position: 'sticky', top: 0, zIndex: 100,
    boxShadow: '0 1px 20px rgba(124,58,237,0.08)',
  },
  navLeft: { display: 'flex', alignItems: 'center', gap: '10px' },
  navLogoMark: {
    width: '34px', height: '34px',
    background: 'linear-gradient(135deg, #9333ea, #7c3aed)',
    borderRadius: '9px',
    display: 'flex', alignItems: 'center', justifyContent: 'center',
    fontSize: '16px',
    boxShadow: '0 2px 8px rgba(124,58,237,0.3)',
  },
  navLogo: { fontSize: '18px', fontWeight: '800', color: '#4c1d95', letterSpacing: '-0.5px' },
  navRight: { display: 'flex', alignItems: 'center', gap: '16px' },
  navUser: { display: 'flex', alignItems: 'center', gap: '10px' },
  avatar: {
    width: '34px', height: '34px',
    background: 'linear-gradient(135deg, #c084fc, #9333ea)',
    borderRadius: '50%',
    display: 'flex', alignItems: 'center', justifyContent: 'center',
    fontSize: '13px', fontWeight: '700', color: '#fff',
    boxShadow: '0 2px 8px rgba(147,51,234,0.3)',
  },
  navName: { fontSize: '14px', fontWeight: '500', color: '#4c1d95' },
  logoutBtn: {
    padding: '7px 16px', borderRadius: '8px',
    border: '1.5px solid #e9d5ff',
    background: '#fff', cursor: 'pointer',
    fontSize: '13px', fontWeight: '500', color: '#7c3aed',
    transition: 'all 0.15s',
  },
  /* Content */
  content: { maxWidth: '960px', margin: '0 auto', padding: '36px 24px' },
  pageHeader: {
    display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start',
    marginBottom: '32px',
  },
  pageTitle: {
    fontSize: '26px', fontWeight: '800', color: '#1e1b4b',
    letterSpacing: '-0.5px', marginBottom: '4px',
  },
  pageSubtitle: { fontSize: '14px', color: '#7c3aed', fontWeight: '500' },
  refreshBtn: {
    padding: '9px 18px', borderRadius: '9px',
    border: '1.5px solid #e9d5ff',
    background: '#fff', cursor: 'pointer',
    fontSize: '13px', fontWeight: '600', color: '#7c3aed',
    transition: 'all 0.15s',
    boxShadow: '0 1px 4px rgba(124,58,237,0.08)',
  },
  /* Stats */
  statsRow: {
    display: 'grid', gridTemplateColumns: 'repeat(4, 1fr)',
    gap: '16px', marginBottom: '36px',
  },
  statCard: {
    background: '#fff', borderRadius: '14px',
    padding: '22px 20px',
    boxShadow: '0 2px 12px rgba(124,58,237,0.07)',
    borderTop: '3px solid transparent',
    cursor: 'default',
  },
  statIcon: { fontSize: '22px', marginBottom: '10px' },
  statNum: {
    fontSize: '30px', fontWeight: '800', color: '#1e1b4b',
    letterSpacing: '-1px', marginBottom: '4px',
  },
  statLabel: { fontSize: '12px', color: '#9ca3af', fontWeight: '600', textTransform: 'uppercase', letterSpacing: '0.06em', marginBottom: '14px' },
  statBar: { height: '4px', borderRadius: '2px', overflow: 'hidden' },
  statBarFill: { height: '100%', borderRadius: '2px', transition: 'width 0.6s ease' },
  /* Section header */
  sectionHeader: {
    display: 'flex', alignItems: 'center', gap: '12px', marginBottom: '16px',
  },
  sectionTitle: { fontSize: '18px', fontWeight: '700', color: '#1e1b4b' },
  sectionCount: {
    background: '#ede9fe', color: '#7c3aed',
    fontSize: '12px', fontWeight: '700',
    padding: '3px 10px', borderRadius: '20px',
  },
  /* Job list */
  jobList: { display: 'flex', flexDirection: 'column', gap: '10px' },
  emptyState: {
    background: '#fff', borderRadius: '14px',
    padding: '48px', textAlign: 'center',
    boxShadow: '0 2px 12px rgba(124,58,237,0.07)',
  },
  emptyIcon: { fontSize: '36px', marginBottom: '12px' },
  emptyText: { fontSize: '15px', color: '#9ca3af' },
  /* Job card */
  jobCard: {
    background: '#fff', borderRadius: '14px',
    boxShadow: '0 2px 12px rgba(124,58,237,0.06)',
    display: 'flex', overflow: 'hidden',
    border: '1px solid #f3e8ff',
  },
  jobAccent: { width: '4px', flexShrink: 0 },
  jobBody: { flex: 1, padding: '18px 20px' },
  jobTop: {
    display: 'flex', justifyContent: 'space-between',
    alignItems: 'flex-start', marginBottom: '14px',
  },
  jobInfo: { flex: 1, minWidth: 0, paddingRight: '16px' },
  jobTitle: {
    fontSize: '15px', fontWeight: '700', color: '#1e1b4b',
    marginBottom: '5px', letterSpacing: '-0.2px',
  },
  jobMeta: { display: 'flex', alignItems: 'center', gap: '6px', flexWrap: 'wrap' },
  jobCompany: { fontSize: '13px', fontWeight: '600', color: '#7c3aed' },
  metaDot: { color: '#d1d5db', fontSize: '12px' },
  jobLocation: { fontSize: '13px', color: '#9ca3af' },
  jobBadges: { display: 'flex', alignItems: 'center', gap: '8px', flexShrink: 0 },
  scoreBadge: {
    display: 'flex', alignItems: 'center', gap: '5px',
    padding: '4px 10px', borderRadius: '20px',
    fontSize: '12px', fontWeight: '700',
  },
  scoreDot: { width: '7px', height: '7px', borderRadius: '50%', flexShrink: 0 },
  statusBadge: {
    padding: '4px 10px', borderRadius: '20px',
    fontSize: '12px', fontWeight: '600',
  },
  jobActions: { display: 'flex', alignItems: 'center', justifyContent: 'flex-end', gap: '10px' },
  viewBtn: {
    padding: '8px 16px', borderRadius: '8px',
    border: '1.5px solid #e9d5ff', color: '#7c3aed',
    textDecoration: 'none', fontSize: '13px', fontWeight: '600',
    transition: 'all 0.15s', background: '#faf5ff',
    display: 'inline-block',
  },
  applyBtn: {
    padding: '8px 18px', borderRadius: '8px',
    border: 'none', fontSize: '13px', fontWeight: '700',
    transition: 'all 0.15s',
    display: 'inline-flex', alignItems: 'center', gap: '6px',
  },
  btnLoadingInner: { display: 'flex', alignItems: 'center', gap: '6px' },
  btnSpinner: {
    width: '13px', height: '13px',
    border: '2px solid rgba(255,255,255,0.3)',
    borderTopColor: '#fff', borderRadius: '50%',
    display: 'inline-block',
    animation: 'spin 0.7s linear infinite',
  },
}
