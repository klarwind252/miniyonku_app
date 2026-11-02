
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <cstdlib>
#include <vector>
#include <map>
// ---- スタブ ----
static uint32_t g_now=0; static uint32_t millis(){return g_now;}
struct Ser{ template<class...A> void printf(const char*,A...){} void println(const char*){} } Serial;
namespace proto { enum{PT_EVENT=1,PT_LOST_NOTICE=2}; struct EventBody{uint8_t lane,quality,miss,_pad;uint64_t t_us,t_us_b;}; struct LostNoticeBody{uint8_t lane;uint32_t dropped_seq;}; }
struct Sent{ uint8_t type; uint32_t seq; uint32_t t; };
static std::vector<Sent> g_air; static uint32_t g_seq=0;
namespace mesh { static void send(uint8_t type,uint8_t,const void*,size_t){ g_seq++; g_air.push_back({type,g_seq,g_now}); } static uint32_t last_seq(){return g_seq;} }
// ---- 実ソース抜粋 ----
struct Pending {
  bool     used;
  uint32_t seq;         // 直近に送ったヘッダseq（LOST通知用）
  uint32_t seqs[4];     // ★20261031 A1：試行ごとのseq（ACKは「どの試行に対する受領」でも照合して外す）
  uint8_t  tries;
  uint32_t last_ms;
  proto::EventBody body;
};
static constexpr int    PENDING_MAX = 8;
static constexpr uint8_t MAX_TRIES  = 4;
static constexpr uint32_t RESEND_MS = 60;     // ACK来なければ60 msで再送
static Pending s_pending[PENDING_MAX];
static void handle_ack(uint32_t acked_seq_in) {
  struct { uint32_t reserved1; } h{acked_seq_in};
  
      uint32_t acked_seq = h.reserved1;                // GWが載せた受領seq
      // ★20261031 A1：旧実装は「最新の試行のseq」だけ照合していた。第k回のACKが第k+1回送信後
      //   （60ms超の遅れ）に届くと照合に失敗して無視され、不要な再送→GWに同じ通過が別seqで
      //   重複記録／4回とも擦れ違うと偽LOST、になっていた。全試行のseqで照合する。
      for (int i = 0; i < PENDING_MAX; i++) {
        if (!s_pending[i].used) continue;
        for (uint8_t k = 0; k < s_pending[i].tries && k < 4; k++)
          if (s_pending[i].seqs[k] == acked_seq) { s_pending[i].used = false; break; }
      }
    
}
static void service_pending() {
  uint32_t nowm = millis();
  for (int i = 0; i < PENDING_MAX; i++) {
    Pending& p = s_pending[i];
    if (!p.used) continue;
    if (p.last_ms != 0 && (nowm - p.last_ms) < RESEND_MS) continue;

    if (p.tries >= MAX_TRIES) {
      p.used = false;
      Serial.printf("[WARN] event give up lane=%u\n", p.body.lane);
      // C1(24.14/案A・27章)：あきらめた瞬間、GWへLost通知を1本送る（best-effort）。
      //   この通知自体は再送管理しない。届けばGWが Lost×/セクター通信不良を表示。
      //   落ちても既存のNode×や後続EVENTのseq飛びで異常自体は別途露見しうる。
      proto::LostNoticeBody ln{};
      ln.lane        = p.body.lane;
      ln.dropped_seq = p.seq;
      mesh::send(proto::PT_LOST_NOTICE, 6 /*GW*/, &ln, sizeof(ln));
      continue;
    }
    mesh::send(proto::PT_EVENT, 6 /*GW*/, &p.body, sizeof(p.body));
    p.seq     = mesh::last_seq();   // 直近に送ったseq
    if (p.tries < 4) p.seqs[p.tries] = p.seq;   // ★A1：試行ごとのseqを記録
    p.tries  += 1;
    p.last_ms = nowm;
  }
}

// ---- シミュレーション ----
// 1通過を送る。p_ev=EVENT到達率, p_ack=ACK到達率, ack_delay=ACKの遅れ(ms)。GW側で受理した回数(=重複記録の元)と LOST の有無を返す。
struct Out{ int gw_received; bool lost; bool acked_any; };
static Out one_pass(double p_ev,double p_ack,uint32_t ack_delay){
  memset(s_pending,0,sizeof(s_pending)); g_air.clear();
  s_pending[0].used=true; s_pending[0].tries=0; s_pending[0].last_ms=0; s_pending[0].body.lane=1;
  Out o{0,false,false}; std::vector<std::pair<uint32_t,uint32_t>> acks; // (arrive_t, seq)
  for(g_now=1; g_now<2000; g_now++){
    service_pending();
    for(auto&s: g_air){ if(s.type==proto::PT_EVENT && s.t==g_now){ if((double)rand()/RAND_MAX<p_ev){ o.gw_received++; if((double)rand()/RAND_MAX<p_ack) acks.push_back({g_now+ack_delay,s.seq}); } } if(s.type==proto::PT_LOST_NOTICE && s.t==g_now) o.lost=true; }
    for(auto&a: acks) if(a.first==g_now){ handle_ack(a.second); o.acked_any=true; }
    if(!s_pending[0].used && acks.empty()) { /* 送り切り or give up */ }
    if(!s_pending[0].used && g_now>400) break;
  }
  return o;
}
int main(int argc,char**argv){
  srand(12345); int N=20000;
  printf("%-22s %6s %6s %8s %10s %10s\n","condition","p_ev","p_ack","ackDelay","falseLOST%","dupRecords/pass");
  struct C{const char*n;double pe,pa;uint32_t d;} cs[]={{"clean",1.0,1.0,5},{"ack delayed 70ms",1.0,1.0,70},{"ack delayed 130ms",1.0,1.0,130},{"p=0.7 d=20",0.7,0.7,20},{"p=0.7 d=70",0.7,0.7,70},{"p=0.5 d=70",0.5,0.5,70}};
  for(auto&c:cs){ int fl=0; long dup=0; for(int i=0;i<N;i++){ Out o=one_pass(c.pe,c.pa,c.d); if(o.lost && o.gw_received>0) fl++; if(o.gw_received>1) dup+=o.gw_received-1; }
    printf("%-22s %6.2f %6.2f %8u %9.2f%% %10.3f\n",c.n,c.pe,c.pa,c.d,100.0*fl/N,(double)dup/N); }
}
