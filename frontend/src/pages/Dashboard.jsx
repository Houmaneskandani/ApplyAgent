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
    if (score >= 9) return '#22c55e'
    if (score >= 7) return '#f59e0b'
    return '#94a3b8'
  }

  if (loading) return <div style={styles.loading}>Loading...</div>

  return (
    <div style={styles.container}>
      <div style={styles.navbar}>
        <span style={styles.logo}>JobBot</span>
        <div style={styles.navRight}>
          <span style={styles.navName}>Hi, {name}</span>
          <button onClick={logout} style={styles.logoutBtn}>Logout</button>
        </div>
      </div>

      <div style={styles.content}>
        {stats && (
          <div style={styles.statsRow}>
            <div style={styles.statCard}>
              <div style={styles.statNum}>{stats.total_jobs?.toLocaleString()}</div>
              <div style={styles.statLabel}>Jobs found</div>
            </div>
            <div style={styles.statCard}>
              <div style={styles.statNum}>{stats.strong_matches}</div>
              <div style={styles.statLabel}>Strong matches</div>
            </div>
            <div style={styles.statCard}>
              <div style={{...styles.statNum, color: '#22c55e'}}>{stats.applied}</div>
              <div style={styles.statLabel}>Applied</div>
            </div>
            <div style={styles.statCard}>
              <div style={styles.statNum}>{stats.scored}</div>
              <div style={styles.statLabel}>Scored</div>
            </div>
          </div>
        )}

        <h2 style={styles.sectionTitle}>Top matches</h2>
        <div style={styles.jobList}>
          {jobs.map(job => (
            <div key={job.id} style={styles.jobCard}>
              <div style={styles.jobTop}>
                <div>
                  <div style={styles.jobTitle}>{job.title}</div>
                  <div style={styles.jobCompany}>{job.company} · {job.location || 'Location not listed'}</div>
                </div>
                <div style={styles.jobRight}>
                  <div style={{...styles.score, background: scoreColor(job.score)}}>
                    {job.score}/10
                  </div>
                  <span style={{...styles.status, background: job.status === 'applied' ? '#dcfce7' : '#f1f5f9', color: job.status === 'applied' ? '#15803d' : '#64748b'}}>
                    {job.status}
                  </span>
                </div>
              </div>
              <div style={styles.jobBottom}>
                <a href={job.url} target="_blank" rel="noreferrer" style={styles.viewLink}>
                  View job
                </a>
                <button
                  onClick={() => applyToJob(job.id)}
                  disabled={applying === job.id || job.status === 'applied'}
                  style={{...styles.applyBtn, opacity: job.status === 'applied' ? 0.5 : 1}}
                >
                  {applying === job.id ? 'Starting...' : job.status === 'applied' ? 'Applied ✓' : 'Auto Apply'}
                </button>
              </div>
            </div>
          ))}
        </div>
      </div>
    </div>
  )
}

const styles = {
  container: { minHeight: '100vh', background: '#f8fafc' },
  loading: { display: 'flex', alignItems: 'center', justifyContent: 'center', height: '100vh', fontSize: '18px', color: '#888' },
  navbar: { background: '#fff', borderBottom: '1px solid #e2e8f0', padding: '0 32px', height: '60px', display: 'flex', alignItems: 'center', justifyContent: 'space-between' },
  logo: { fontSize: '20px', fontWeight: '700', color: '#111' },
  navRight: { display: 'flex', alignItems: 'center', gap: '16px' },
  navName: { color: '#555', fontSize: '14px' },
  logoutBtn: { padding: '7px 14px', borderRadius: '6px', border: '1px solid #ddd', background: '#fff', cursor: 'pointer', fontSize: '13px' },
  content: { maxWidth: '900px', margin: '0 auto', padding: '32px 16px' },
  statsRow: { display: 'grid', gridTemplateColumns: 'repeat(4, 1fr)', gap: '16px', marginBottom: '32px' },
  statCard: { background: '#fff', borderRadius: '12px', padding: '20px', textAlign: 'center', boxShadow: '0 1px 4px rgba(0,0,0,0.06)' },
  statNum: { fontSize: '28px', fontWeight: '700', color: '#111' },
  statLabel: { fontSize: '13px', color: '#888', marginTop: '4px' },
  sectionTitle: { fontSize: '18px', fontWeight: '600', marginBottom: '16px', color: '#111' },
  jobList: { display: 'flex', flexDirection: 'column', gap: '12px' },
  jobCard: { background: '#fff', borderRadius: '12px', padding: '20px', boxShadow: '0 1px 4px rgba(0,0,0,0.06)' },
  jobTop: { display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start', marginBottom: '14px' },
  jobTitle: { fontSize: '16px', fontWeight: '600', color: '#111', marginBottom: '4px' },
  jobCompany: { fontSize: '14px', color: '#666' },
  jobRight: { display: 'flex', alignItems: 'center', gap: '10px' },
  score: { padding: '4px 10px', borderRadius: '20px', color: '#fff', fontSize: '13px', fontWeight: '700' },
  status: { padding: '4px 10px', borderRadius: '20px', fontSize: '12px', fontWeight: '500', textTransform: 'capitalize' },
  jobBottom: { display: 'flex', justifyContent: 'flex-end', gap: '10px' },
  viewLink: { padding: '8px 16px', borderRadius: '8px', border: '1px solid #ddd', color: '#555', textDecoration: 'none', fontSize: '14px' },
  applyBtn: { padding: '8px 18px', borderRadius: '8px', background: '#111', color: '#fff', border: 'none', fontSize: '14px', fontWeight: '600', cursor: 'pointer' },
}
