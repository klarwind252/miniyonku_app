
#include <cstdint>
#include <cstdio>
#include <vector>
static uint32_t g_now=0; static uint32_t millis(){return g_now;}
static uint32_t g_rand=1; static uint32_t esp_random(){ g_rand=g_rand*1103515245u+12345u; return g_rand; }
struct Ser{ template<class...A> void printf(const char*,A...){} void println(const char*){} } Serial;
namespace proto { enum{CMD_SIGNAL=2}; }
static bool s_contact=true; static inline bool in_contact(){return s_contact;}
static volatile bool s_cmd_acked=false;
struct Tx{uint32_t t,nonce;}; static std::vector<Tx> g_tx;
static void send_command(uint8_t, uint32_t nonce, int shots){ for(int i=0;i<shots;i++) g_tx.push_back({g_now,nonce}); }
static uint8_t  s_pend_code  = 0;      // 0=保留なし
static uint32_t s_pend_nonce = 0;
static uint32_t s_pend_born  = 0;      // 押下時刻
static uint32_t s_pend_last  = 0;      // 直近送信時刻
static uint8_t  s_pend_tries = 0;      // 在圏中の送信回数
static void fire(uint8_t code) {
  uint32_t n = esp_random(); if (!n) n = 1;
  s_pend_code = code; s_pend_nonce = n; s_pend_born = millis(); s_pend_tries = 0; s_pend_last = 0;
  s_cmd_acked = false;
}
static void service_cmd() {
  if (!s_pend_code) return;
  uint32_t now = millis();
  if (s_cmd_acked) { Serial.printf("[CMD] ack tries=%u %lums\n", s_pend_tries, (unsigned long)(now - s_pend_born)); s_pend_code = 0; return; }
  bool contact = in_contact();
  if (contact && s_pend_tries >= 4) { Serial.println("[CMD] give up (no ACK x4)"); s_pend_code = 0; return; }
  if (now - s_pend_born > 2000) { Serial.println("[CMD] give up (out of contact 2s)"); s_pend_code = 0; return; }
  uint32_t gap = contact ? (40u + (esp_random() & 31u)) : 100u;   // 在圏中は40〜71ms・走査中は100ms
  if (s_pend_last != 0 && (now - s_pend_last) < gap) return;
  send_command(s_pend_code, s_pend_nonce, /*shots=*/1);
  s_pend_last = now;
  if (contact) s_pend_tries++;
}

int main(){
  // ケース1：在圏・ACKが毎回落ちる → 最大4回、間隔40〜71ms、nonce同一、give up
  g_tx.clear(); s_contact=true; g_now=1000; fire(proto::CMD_SIGNAL);
  for(;g_now<5000 && s_pend_code; g_now++) service_cmd();
  bool same=true; for(auto&t:g_tx) same &= (t.nonce==g_tx[0].nonce);
  printf("case1 no-ACK in contact: sends=%zu nonce_same=%d gaps=",g_tx.size(),(int)same); for(size_t i=1;i<g_tx.size();i++) printf("%u ",g_tx[i].t-g_tx[i-1].t); printf("\n");
  // ケース2：在圏・2回目の送信後にACK → 送信は2回で止まる
  g_tx.clear(); g_now=1000; fire(proto::CMD_SIGNAL);
  for(;g_now<5000 && s_pend_code; g_now++){ service_cmd(); if(g_tx.size()==2 && g_now>g_tx[1].t+5) s_cmd_acked=true; }
  printf("case2 ACK after 2nd: sends=%zu\n",g_tx.size());
  // ケース3：走査中(在圏外)で押下 → 100ms間隔で保留再送、1.2秒後に在圏復帰→ACK → 届く
  g_tx.clear(); s_contact=false; g_now=1000; fire(proto::CMD_SIGNAL); uint32_t t_ack=0;
  for(;g_now<5000 && s_pend_code; g_now++){ if(g_now==2200) s_contact=true; service_cmd(); if(s_contact && !g_tx.empty() && g_tx.back().t>=2200 && !s_cmd_acked){ s_cmd_acked=true; t_ack=g_now; } }
  printf("case3 out-of-contact then recover: sends=%zu delivered_at=+%ums (press at 1000, recover at 2200)\n",g_tx.size(),t_ack-1000);
  // ケース4：在圏外のまま → 2秒で諦める
  g_tx.clear(); s_contact=false; g_now=1000; fire(proto::CMD_SIGNAL); uint32_t t_end=0;
  for(;g_now<6000 && s_pend_code; g_now++){ service_cmd(); t_end=g_now; }
  printf("case4 never recover: sends=%zu gave_up_after=%ums\n",g_tx.size(),t_end-1000);
}
