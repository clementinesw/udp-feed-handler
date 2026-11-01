// feed_handler.sv
// cut-through udp market data feed handler, sits behind a 10G ethernet MAC (64-bit AXI4-Stream, 156.25 MHz)
// checks the headers beat by beat as they come in, decodes one 14-byte quote and sends a tick 1 cycle after the last message byte
// doesnt wait for the FCS, if the MAC says the frame was bad at the end it sends tick_cancel instead
// byte 0 of each beat is in tdata[7:0] (lane 0), network fields are big-endian

module feed_handler (
    input  logic        clk,
    input  logic        rst,                //synchronous, active high

    input  logic [63:0] s_axis_tdata,       //AXI4-Stream from the MAC, no tready so we have to take a beat every cycle
    input  logic [7:0]  s_axis_tkeep,       //1 bit per lane, 1 = real byte
    input  logic        s_axis_tvalid,
    input  logic        s_axis_tlast,       //last beat of the frame
    input  logic        s_axis_tuser,       //only on the last beat, 1 = bad FCS

    input  logic [31:0] cfg_dst_ip,         //the feed we listen to, set before traffic starts
    input  logic [15:0] cfg_dst_port,

    output logic        tick_valid,         //1 cycle pulse per accepted quote
    output logic        tick_side,          //0 = bid, 1 = ask
    output logic [31:0] tick_instrument,
    output logic [31:0] tick_price,
    output logic [31:0] tick_qty,
    output logic        tick_cancel         //the frame of the last tick had a bad FCS
);

    logic [7:0][7:0] lane;                  //splits the bus into 8 bytes, lane[0] = tdata[7:0]
    assign lane = s_axis_tdata;

    logic last_beat;                        //valid beat that ends the frame
    assign last_beat = s_axis_tvalid && s_axis_tlast;

    localparam logic [15:0] ETHERTYPE_IPV4 = 16'h0800;
    localparam logic [7:0]  IPV4_NO_OPTS   = 8'h45;    //version 4, IHL 5 so a 20 byte header
    localparam logic [7:0]  PROTO_UDP      = 8'd17;
    localparam logic [15:0] MIN_UDP_LEN    = 16'd22;   //8 byte udp header + 14 byte message
    localparam logic [7:0]  MSG_QUOTE      = 8'h51;    //'Q'
    localparam logic [7:0]  SIDE_BID       = 8'h42;    //'B'
    localparam logic [7:0]  SIDE_ASK       = 8'h53;    //'S'
    localparam logic [2:0]  LAST_BEAT      = 3'd7;     //counter stops here

    // beat counter
    logic [2:0] beat_cnt;                   //which beat of the frame is on the bus, 7 means 7 or later

    always_ff @(posedge clk) begin
        if (rst) begin
            beat_cnt <= '0;
        end
        else begin
            if (last_beat)                                      //end of the frame, next beat is beat 0
                beat_cnt <= 3'b0;
            else if (beat_cnt < LAST_BEAT && s_axis_tvalid)     //count on valid beats only, stops at 7 so it doesnt wrap
                beat_cnt <= beat_cnt + 3'b1;
        end
    end

    // header checks
    logic beat_ok;                          //do the fields on this beat pass
    logic match_q;                          //has every beat passed so far in this frame

    always_comb begin
        case (beat_cnt)
            3'd1: beat_ok = ({lane[4], lane[5]} == ETHERTYPE_IPV4)          //bytes 12-13, checking that EtherType = x0800
                         && (lane[6] == IPV4_NO_OPTS);                       //byte 14, checking that IP version and header length is correct
            3'd2: beat_ok = ({lane[4][5:0], lane[5]} == 14'b0)              //byte 20 and 21, checking that the message isn't fragmented (no offset and no more follow)
                         && (lane[7] == PROTO_UDP);                          //byte 23, checking that it is UDP protocol
            3'd3: beat_ok = ({lane[6], lane[7]} == cfg_dst_ip[31:16]);      //byte 30 and 31, checking the MSB of correct dest ip
            3'd4: beat_ok = ({lane[0], lane[1]} == cfg_dst_ip[15:0])        //byte 32 and 33, checking the LSB of correct dest ip
                         && ({lane[4], lane[5]} == cfg_dst_port)             //byte 36 and 37, checking the correct dest port
                         && ({lane[6], lane[7]} >= MIN_UDP_LEN);             //byte 38 and 39, checking the correct length of message (>=22)
            3'd5: beat_ok = (lane[2] == MSG_QUOTE)                          //byte 42, checking the correct message type
                         && ((lane[3] == SIDE_ASK) || (lane[3] == SIDE_BID)); //byte 43, checking that its a bid or ask
            default: beat_ok = 1'b1;                                         //beats 0, 6, 7+, nothing to check
        endcase
    end

    always_ff @(posedge clk) begin
        if (rst) begin                          //rst sets match_q to 1 so it can keep reading
            match_q <= 1'b1;
        end
        else if (last_beat)                     //last beat of the frame, set back to 1 ready for the next frame
            match_q <= 1'b1;
        else if (s_axis_tvalid)                 //only update on valid beats, holds its value during gaps
            match_q <= match_q && beat_ok;      //stays 1 only if every beat so far passed, one fail keeps it at 0 until tlast
    end

    // message fields
    logic        side_q;                    //side and instrument come on beat 5, held for one beat until price and qty come on beat 6
    logic [31:0] instrument_q;

    always_ff @(posedge clk) begin
        if (beat_cnt == 3'd5 && s_axis_tvalid) begin                //capture on the real beat 5
            side_q       <= (lane[3] == SIDE_ASK);                  //byte 43, 1 = ask, 0 = bid
            instrument_q <= {lane[4], lane[5], lane[6], lane[7]};   //bytes 44-47
        end
    end

    // output
    logic msg_beat;                         //beat 6 of a frame that has matched so far
    logic bad_end;                          //this beat ends a corrupt frame
    logic tick_sent_q;                      //a tick went out for this frame

    assign msg_beat = beat_cnt == 3'd6 && s_axis_tkeep == 8'hFF && s_axis_tvalid && match_q;   //valid beat 6 with all 8 lanes, only care about it if the frame matches
    assign bad_end  = last_beat && s_axis_tuser;                                                //last beat and the MAC says the FCS is bad

    always_ff @(posedge clk) begin
        if (rst) begin
            tick_valid  <= 1'b0;
            tick_cancel <= 1'b0;
            tick_sent_q <= 1'b0;
        end
        else begin
            tick_valid  <= msg_beat && !bad_end;        //1 cycle after beat 6, dont send if the frame already ends corrupt on beat 6
            tick_cancel <= bad_end && tick_sent_q;      //only cancel if this frame actually sent a tick
            if (last_beat)                              //cleared at the end of every frame, wins if the tick and tlast are on the same beat
                tick_sent_q <= 1'b0;
            else if (msg_beat)                          //set on the same edge as tick_valid and holds until tlast
                tick_sent_q <= 1'b1;
        end
    end

    always_ff @(posedge clk) begin                                  //no reset, only read when tick_valid = 1
        if (msg_beat) begin
            tick_side       <= side_q;
            tick_instrument <= instrument_q;
            tick_price      <= {lane[0], lane[1], lane[2], lane[3]};    //bytes 48-51
            tick_qty        <= {lane[4], lane[5], lane[6], lane[7]};    //bytes 52-55
        end
    end

endmodule


