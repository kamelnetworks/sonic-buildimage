// Dependency fakes for executing the actual patched method bodies without a SAI.
#include <cassert>
#include <cstdint>
#include <map>
#include <memory>
#include <stdexcept>
#include <string>
#include <vector>
#define _In_
#define _Out_
#define SWSS_LOG_ENTER() ((void)0)
#define SWSS_LOG_ERROR(...) ((void)0)
#define SWSS_LOG_WARN(...) ((void)0)
#define SWSS_LOG_THROW(...) throw std::runtime_error("unexpected Redis reply")
using sai_object_id_t = uint64_t;
using sai_status_t = int;
using sai_object_type_t = int;
using sai_bulk_op_error_mode_t = int;
struct sai_attribute_t {};
constexpr int SAI_STATUS_SUCCESS=0, SAI_STATUS_FAILURE=-1,
    SAI_STATUS_NOT_IMPLEMENTED=-2, SAI_STATUS_NOT_SUPPORTED=-3;
constexpr int SAI_OBJECT_TYPE_PORT=1, SAI_NULL_OBJECT_ID=0;
void sai_deserialize_object_id(const std::string& s, sai_object_id_t& id) { id=std::stoull(s); }
std::string sai_serialize_object_id(sai_object_id_t id) { return std::to_string(id); }
std::string sai_serialize_object_type(int) { return "PORT"; }
namespace saimeta {
struct SaiAttributeList {
    uint32_t get_attr_count() const { return 0; }
    const sai_attribute_t* get_attr_list() const { return nullptr; }
};
}
struct VidManager { static sai_object_id_t switchIdQuery(sai_object_id_t) { return 1; } };
struct Translator {
    std::vector<sai_object_id_t> rids, vids;
    sai_object_id_t translateVidToRid(sai_object_id_t) { return 10; }
    void insertRidsAndVids(size_t count, const sai_object_id_t* r, const sai_object_id_t* v) {
        for(size_t i=0;i<count;++i) { rids.push_back(r[i]); vids.push_back(v[i]); }
    }
};
struct Vendor {
    std::vector<int> result;
    int status=SAI_STATUS_SUCCESS;
    int bulkCreate(int, sai_object_id_t, uint32_t count, const uint32_t*,
                   const sai_attribute_t**, int, sai_object_id_t* rids, int* statuses) {
        for(size_t i=0;i<count;++i) { rids[i]=100+i; statuses[i]=result.at(i); }
        return status;
    }
};
struct Switch {
    std::vector<sai_object_id_t> discovered;
    void onPostPortsCreate(size_t count, const sai_object_id_t* rids) {
        assert(count != 0); // Empty discovery was the crash trigger.
        discovered.assign(rids, rids+count);
    }
};
struct Syncd {
    std::shared_ptr<Translator> m_translator=std::make_shared<Translator>();
    std::shared_ptr<Vendor> m_vendorSai=std::make_shared<Vendor>();
    std::map<sai_object_id_t,std::shared_ptr<Switch>> m_switches;
    int processBulkOidCreate(int,int,const std::vector<std::string>&,
            const std::vector<std::shared_ptr<saimeta::SaiAttributeList>>&,std::vector<int>&);
};
constexpr int REDIS_REPLY_ARRAY=1, REDIS_REPLY_STRING=2, REDIS_REPLY_NIL=3;
const char* RIDTOVID="RIDTOVID";
struct Reply { int type=REDIS_REPLY_ARRAY; size_t elements=0; Reply** element=nullptr; char* str=nullptr; };
struct Db {};
namespace swss {
std::vector<std::string> command;
int redis_calls=0;
struct RedisCommand { void format(const std::vector<std::string>& args) { command=args; } };
struct RedisReply {
    Reply reply;
    Reply value;
    Reply* values[1];
    RedisReply(Db*, const RedisCommand&) {
        ++redis_calls;
        assert(command.size() >= 3); // Redis rejects HMGET without a field.
        value.type=REDIS_REPLY_STRING; value.str=const_cast<char*>("900");
        values[0]=&value; reply.elements=1; reply.element=values;
    }
    Reply* getContext() { return &reply; }
};
}
struct RedisClient {
    std::shared_ptr<Db> m_dbAsic=std::make_shared<Db>();
    void getVidsForRids(size_t,const sai_object_id_t*,sai_object_id_t*);
};
#include "methods.inc"

int main() {
    for(auto outcome : std::vector<std::vector<int>>{{-1,-1},{0,-1},{-1,0},{0,0}}) {
        Syncd syncd;
        auto sw=std::make_shared<Switch>(); syncd.m_switches[1]=sw;
        syncd.m_vendorSai->result=outcome;
        syncd.m_vendorSai->status=outcome==std::vector<int>{0,0}?0:-1;
        std::vector<int> statuses(2);
        std::vector<std::shared_ptr<saimeta::SaiAttributeList>> attrs(2);
        for(auto& a:attrs) a=std::make_shared<saimeta::SaiAttributeList>();
        auto status=syncd.processBulkOidCreate(SAI_OBJECT_TYPE_PORT,0,{"1","2"},attrs,statuses);
        assert(status==syncd.m_vendorSai->status && statuses==outcome);
        std::vector<sai_object_id_t> expectedRids, expectedVids;
        for(size_t i=0;i<2;++i) if(outcome[i]==0) { expectedRids.push_back(100+i); expectedVids.push_back(1+i); }
        assert(syncd.m_translator->rids==expectedRids);
        assert(syncd.m_translator->vids==expectedVids);
        assert(sw->discovered==expectedRids);
    }
    for(int apiStatus : {SAI_STATUS_NOT_IMPLEMENTED, SAI_STATUS_NOT_SUPPORTED}) {
        Syncd syncd; syncd.m_vendorSai->status=apiStatus; syncd.m_vendorSai->result={-1};
        std::vector<int> statuses(1);
        std::vector<std::shared_ptr<saimeta::SaiAttributeList>> attrs{std::make_shared<saimeta::SaiAttributeList>()};
        assert(syncd.processBulkOidCreate(1,0,{"1"},attrs,statuses)==apiStatus);
        assert(syncd.m_translator->rids.empty());
    }
    Syncd other; other.m_vendorSai->result={0};
    std::vector<int> statuses(1);
    std::vector<std::shared_ptr<saimeta::SaiAttributeList>> attrs{std::make_shared<saimeta::SaiAttributeList>()};
    assert(other.processBulkOidCreate(99,0,{"1"},attrs,statuses)==0);
    assert(other.m_translator->rids==std::vector<sai_object_id_t>{100});
    assert(other.processBulkOidCreate(1,0,{}, {},statuses)==SAI_STATUS_FAILURE);
    RedisClient client;
    client.getVidsForRids(0,nullptr,nullptr);
    assert(swss::redis_calls==0);
    sai_object_id_t rid=100, vid=0;
    client.getVidsForRids(1,&rid,&vid);
    assert(swss::redis_calls==1 && vid==900);
    assert((swss::command==std::vector<std::string>{"HMGET","RIDTOVID","100"}));
}
